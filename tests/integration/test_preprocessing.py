# ruff: noqa: B008
"""Integration tests for pre-processing (§7.10.2 — acceptance criterion 6).

Pre-processing never blocks reading: a study is openable with
``preprocessingState: "PENDING"``, and a failed stage degrades to empty
findings with a reason, never an error.  Tested by failing each stage in turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import ResidencyPolicy
from app.models.preprocessing import PreprocessingState, StageStatus
from app.models.study import StudyRecord
from app.repositories.base import InMemoryDocumentStore
from app.segmentation.registry import SegmentationRegistry
from app.services.preprocessing.orchestrator import PreprocessingOrchestrator
from app.services.preprocessing.priors import PriorsHandler
from app.services.preprocessing.segmentation_dispatcher import (
    SegmentationDispatcher,
)
from app.services.preprocessing.volumetry import VolumetryHandler


def _study(
    modality: str = "CT",
    body_part: str = "CHEST",
    instances: int = 412,
) -> StudyRecord:
    return StudyRecord(
        study_id="st_test",
        modality=modality,
        body_part=body_part,
        instance_count=instances,
        patient_key="pk_test",
        tenant_id="default",
    )


def _dispatcher(
    runtime: str = "cpu_fast",
    policy: ResidencyPolicy = ResidencyPolicy.africa,
) -> SegmentationDispatcher:
    """Build a dispatcher with the given runtime and residency policy."""
    return SegmentationDispatcher(
        SegmentationRegistry(),
        runtime=runtime,
        residency_policy=policy,
    )


def _orchestrator(
    store: InMemoryDocumentStore,
    runtime: str = "cpu_fast",
    policy: ResidencyPolicy = ResidencyPolicy.africa,
    **kwargs: object,
) -> PreprocessingOrchestrator:
    """Build an orchestrator with the given store and dispatcher."""
    return PreprocessingOrchestrator(
        store,
        _dispatcher(runtime, policy),
        **kwargs,  # type: ignore[arg-type]
    )


class _FailingVolumetry(VolumetryHandler):
    """A volumetry handler that always raises."""

    async def run(
        self,
        study: StudyRecord,
        state: PreprocessingState,
        segmentation_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("volumetry crashed")


class _FailingPriors(PriorsHandler):
    """A priors handler that always raises."""

    async def run(self, study: StudyRecord, state: PreprocessingState) -> dict[str, object]:
        raise RuntimeError("priors crashed")


# ---------------------------------------------------------------------------
# Criterion 6 — pre-processing never blocks reading
# ---------------------------------------------------------------------------
class TestStudyOpenableWithPending:
    """A study is openable with preprocessingState: PENDING."""

    def test_pending_state_before_run(self) -> None:
        store = InMemoryDocumentStore()
        orch = _orchestrator(
            store,
        )
        state = asyncio.run(orch.get_state("st_test"))
        assert state.state == "PENDING"
        # The study is openable — no error, just a PENDING state.

    def test_get_state_does_not_run_pipeline(self) -> None:
        store = InMemoryDocumentStore()
        orch = _orchestrator(
            store,
        )
        state = asyncio.run(orch.get_state("st_test"))
        assert state.state == "PENDING"
        assert state.stages == {}


class TestStageDegradation:
    """A failed stage degrades to empty findings with a reason, never an error."""

    def test_us_study_degrades_to_unavailable(self) -> None:
        """US has no model — segmentation degrades to UNAVAILABLE."""
        store = InMemoryDocumentStore()
        orch = _orchestrator(
            store,
        )
        study = _study(modality="US", body_part="ABDOMEN")
        state = asyncio.run(orch.run(study))
        seg = state.stages["segmentation"]
        assert seg.status == StageStatus.UNAVAILABLE
        assert seg.reason == "NO_MODEL_AVAILABLE"
        # Never an error — the pipeline completes with PARTIAL/UNAVAILABLE state.
        assert state.state in ("PARTIAL", "UNAVAILABLE")

    def test_cr_chest_degrades_to_unavailable(self) -> None:
        """CR CHEST has no model — segmentation degrades to UNAVAILABLE."""
        store = InMemoryDocumentStore()
        orch = _orchestrator(
            store,
        )
        study = _study(modality="CR", body_part="CHEST")
        state = asyncio.run(orch.run(study))
        seg = state.stages["segmentation"]
        assert seg.status == StageStatus.UNAVAILABLE
        assert seg.reason == "NO_MODEL_AVAILABLE"

    def test_mr_brain_on_cpu_fast_degrades_to_unavailable_in_region(self) -> None:
        """MR BRAIN requires GPU — on cpu_fast it degrades to UNAVAILABLE_IN_REGION."""
        store = InMemoryDocumentStore()
        orch = _orchestrator(
            store,
        )
        study = _study(modality="MR", body_part="BRAIN")
        state = asyncio.run(orch.run(study))
        seg = state.stages["segmentation"]
        assert seg.status == StageStatus.UNAVAILABLE
        assert seg.reason == "UNAVAILABLE_IN_REGION"

    def test_ct_chest_completes_on_cpu_fast(self) -> None:
        """CT CHEST is available on cpu_fast — all stages complete."""
        store = InMemoryDocumentStore()
        orch = _orchestrator(
            store,
        )
        study = _study(modality="CT", body_part="CHEST")
        state = asyncio.run(orch.run(study))
        assert state.stages["segmentation"].status == StageStatus.COMPLETE
        assert state.state == "COMPLETE"

    def test_failing_volumetry_degrades_not_crashes(self) -> None:
        """A crashing volumetry handler degrades to FAILED, not an exception."""
        store = InMemoryDocumentStore()
        orch = _orchestrator(store, volumetry=_FailingVolumetry())
        study = _study(modality="CT", body_part="CHEST")
        state = asyncio.run(orch.run(study))
        # Segmentation completes, volumetry fails.
        assert state.stages["segmentation"].status == StageStatus.COMPLETE
        assert state.stages["volumetry"].status == StageStatus.FAILED
        assert state.state == "PARTIAL"

    def test_failing_priors_degrades_not_crashes(self) -> None:
        """A crashing priors handler degrades to FAILED, not an exception."""
        store = InMemoryDocumentStore()
        orch = _orchestrator(store, priors=_FailingPriors())
        study = _study(modality="CT", body_part="CHEST")
        state = asyncio.run(orch.run(study))
        assert state.stages["priors"].status == StageStatus.FAILED
        assert state.state == "PARTIAL"


class TestRetryStage:
    """retry_stage re-runs a single failed stage."""

    def test_retry_volumetry_after_failure(self) -> None:
        store = InMemoryDocumentStore()
        disp = _dispatcher()
        # First run with failing volumetry
        orch = PreprocessingOrchestrator(store, disp, volumetry=_FailingVolumetry())
        study = _study(modality="CT", body_part="CHEST")
        state = asyncio.run(orch.run(study))
        assert state.stages["volumetry"].status == StageStatus.FAILED
        # Now retry with a working handler
        orch2 = PreprocessingOrchestrator(store, disp)
        from app.core.auth import SecondFactorState
        from app.core.capabilities import Role
        from tests.conftest import make_user

        user = make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
        state = asyncio.run(orch2.retry_stage(study, "volumetry", user))
        assert state.stages["volumetry"].status in (StageStatus.COMPLETE, StageStatus.SKIPPED)


class TestPreprocessingStatePersistence:
    """State is persisted to the document store."""

    def test_state_persisted_after_run(self) -> None:
        store = InMemoryDocumentStore()
        orch = _orchestrator(
            store,
        )
        study = _study(modality="CT", body_part="CHEST")
        asyncio.run(orch.run(study))
        # State should be in the store
        doc = asyncio.run(store.get("preprocessing_states", "st_test"))
        assert doc is not None
        assert doc["state"] == "COMPLETE"
