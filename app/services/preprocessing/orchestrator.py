"""Pre-processing orchestrator — stage sequencing, idempotent retry (§7.10.2).

The orchestrator runs the pre-processing pipeline (segmentation → volumetry →
priors) for a study.  Each stage degrades gracefully: a failure or unavailable
model never blocks reading; it produces an empty findings panel with a reason
(acceptance criterion 6).

Key properties:
- **Never blocks reading.**  A study is openable with ``preprocessingState:
  "PENDING"``.  ``get_state()`` returns the current state without running
  anything.
- **Idempotent retry.**  ``retry_stage()`` re-runs a single failed stage.
- **Partial-failure degradation.**  A failed stage sets ``FAILED`` (or
  ``UNAVAILABLE``) and the aggregate state becomes ``PARTIAL`` — the findings
  panel shows empty findings with the unavailable reason.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.auth import AuthenticatedUser
from app.models.preprocessing import (
    PreprocessingStageName,
    PreprocessingState,
    StageResult,
    StageStatus,
)
from app.models.study import StudyRecord
from app.repositories.base import DocumentStore
from app.services.preprocessing.priors import PriorsHandler
from app.services.preprocessing.segmentation_dispatcher import (
    ResidencyViolation,
    SegmentationDispatcher,
    SegmentationPlan,
    SegmentationPlanState,
)
from app.services.preprocessing.volumetry import VolumetryHandler

logger = logging.getLogger("vurarad.preprocessing")

PREPROCESSING_COLLECTION = "preprocessing_states"

# The ordered stage list.
STAGE_ORDER: list[PreprocessingStageName] = ["segmentation", "volumetry", "priors"]


# ---------------------------------------------------------------------------
# Stage handler protocol
# ---------------------------------------------------------------------------
StageHandler = Callable[
    ["PreprocessingOrchestrator", StudyRecord, PreprocessingState],
    Awaitable[dict[str, object]],
]


class _SkippedStage:
    """A no-op stage handler for stages not in the default set."""

    def __init__(self, name: str) -> None:
        self._name = name

    async def __call__(
        self,
        orch: PreprocessingOrchestrator,
        study: StudyRecord,
        state: PreprocessingState,
    ) -> dict[str, object]:
        state.stages[self._name] = StageResult(
            status=StageStatus.SKIPPED,
            reason=f"stage '{self._name}' not configured",
        )
        state.state = state.aggregate()
        return {}


class PreprocessingOrchestrator:
    """Sequences pre-processing stages with graceful degradation."""

    def __init__(
        self,
        store: DocumentStore,
        dispatcher: SegmentationDispatcher,
        *,
        volumetry: VolumetryHandler | None = None,
        priors: PriorsHandler | None = None,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._volumetry = volumetry or VolumetryHandler()
        self._priors = priors or PriorsHandler()
        self._last_seg_result: dict[str, object] = {}

    # -- state persistence ---------------------------------------------------
    async def get_state(self, study_id: str) -> PreprocessingState:
        """Return the current pre-processing state (never blocks; never runs)."""
        doc = await self._store.get(PREPROCESSING_COLLECTION, study_id)
        if doc is not None:
            return _state_from_doc(doc)
        # No state yet — return a PENDING state (study is openable immediately).
        return PreprocessingState(study_id=study_id, state="PENDING")

    async def _save_state(self, state: PreprocessingState) -> None:
        state.updated_at = datetime.now(UTC)
        await self._store.set(PREPROCESSING_COLLECTION, state.study_id, state.model_dump())

    # -- run the full pipeline ------------------------------------------------
    async def run(self, study: StudyRecord) -> PreprocessingState:
        """Run all stages in order, degrading gracefully on failure."""
        state = await self.get_state(study.study_id)
        for name in STAGE_ORDER:
            await self._run_stage(name, study, state)
        await self._save_state(state)
        return state

    # -- retry a single stage -------------------------------------------------
    async def retry_stage(
        self,
        study: StudyRecord,
        stage: PreprocessingStageName,
        user: AuthenticatedUser,
    ) -> PreprocessingState:
        """Re-run a single stage (idempotent retry)."""
        state = await self.get_state(study.study_id)
        await self._run_stage(stage, study, state)
        await self._save_state(state)
        return state

    # -- stage execution ------------------------------------------------------
    async def _run_stage(
        self,
        name: PreprocessingStageName,
        study: StudyRecord,
        state: PreprocessingState,
    ) -> None:
        now = datetime.now(UTC)
        # Mark RUNNING
        state.stages[name] = StageResult(status=StageStatus.RUNNING, started_at=now)
        state.state = state.aggregate()
        try:
            if name == "segmentation":
                await self._run_segmentation(study, state)
            elif name == "volumetry":
                seg_result = state.stages.get("segmentation")
                seg_data = self._seg_data(state) if seg_result else None
                await self._volumetry.run(study, state, seg_data)
            elif name == "priors":
                await self._priors.run(study, state)
            else:
                state.stages[name] = StageResult(
                    status=StageStatus.SKIPPED,
                    reason=f"unknown stage '{name}'",
                )
        except ResidencyViolation as exc:
            state.stages[name] = StageResult(
                status=StageStatus.UNAVAILABLE,
                reason="RESIDENCY_VIOLATION",
                error=str(exc),
            )
            logger.warning("preprocessing stage %s blocked by residency: %s", name, exc)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash
            state.stages[name] = StageResult(
                status=StageStatus.FAILED,
                error=str(exc),
            )
            logger.warning("preprocessing stage %s failed: %s", name, exc)
        state.state = state.aggregate()

    async def _run_segmentation(self, study: StudyRecord, state: PreprocessingState) -> None:
        """Resolve and dispatch segmentation, degrading on unavailable."""
        plan: SegmentationPlan = self._dispatcher.resolve_modalities(
            modality=study.modality,
            body_part=study.body_part,
            instance_count=study.instance_count,
        )
        if plan.state == SegmentationPlanState.NO_MODEL_AVAILABLE:
            state.stages["segmentation"] = StageResult(
                status=StageStatus.UNAVAILABLE,
                reason="NO_MODEL_AVAILABLE",
                detail=plan.detail or f"No model for ({study.modality}, {study.body_part})",
            )
            return
        if plan.state == SegmentationPlanState.UNAVAILABLE_IN_REGION:
            state.stages["segmentation"] = StageResult(
                status=StageStatus.UNAVAILABLE,
                reason="UNAVAILABLE_IN_REGION",
                detail=f"Bundle {plan.bundle_id} requires GPU; runtime is {plan.runtime}",
            )
            return
        if plan.state == SegmentationPlanState.DISABLED_BY_LICENCE:
            state.stages["segmentation"] = StageResult(
                status=StageStatus.UNAVAILABLE,
                reason="DISABLED_BY_LICENCE",
                detail="licence gate closed",
            )
            return
        # AVAILABLE — dispatch (residency asserted inside dispatch).
        result = await self._dispatcher.dispatch(plan, study)
        state.stages["segmentation"] = StageResult(
            status=StageStatus.COMPLETE,
            completed_at=datetime.now(UTC),
        )
        # Stash the segmentation result so volumetry can use it.
        state.stages["segmentation"].detail = "complete"
        # Store the raw result on the state via a side channel.
        self._last_seg_result = result

    @staticmethod
    def _seg_data(state: PreprocessingState) -> dict[str, object] | None:
        """Return the segmentation data if the stage completed."""
        result = state.stages.get("segmentation")
        if result is not None and result.status == StageStatus.COMPLETE:
            return {"complete": True}
        return None


def _state_from_doc(doc: dict[str, object]) -> PreprocessingState:
    """Reconstruct a :class:`PreprocessingState` from a stored doc."""
    return PreprocessingState.model_validate(doc)


__all__ = [
    "PREPROCESSING_COLLECTION",
    "PreprocessingOrchestrator",
    "STAGE_ORDER",
]
