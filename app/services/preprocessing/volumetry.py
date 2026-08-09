"""Volumetry — volumes and measurements from accepted segmentation masks.

Runs after segmentation produces masks.  If segmentation was unavailable or
failed, volumetry is skipped (it has nothing to measure).  The handler is a
stage in the orchestrator pipeline (§7.10.2).
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.preprocessing import PreprocessingState, StageResult, StageStatus
from app.models.study import StudyRecord

logger = logging.getLogger("vurarad.preprocessing.volumetry")


class VolumetryUnavailable(Exception):  # noqa: N818
    """Raised when volumetry cannot run (no segmentation masks)."""


class VolumetryHandler:
    """Compute volumes and measurements from segmentation masks.

    In dev/CI this is a deterministic stub that produces empty measurements —
    the real implementation would walk the mask objects and compute organ
    volumes.  The handler never raises on missing masks; it records a SKIPPED
    stage result so the pipeline degrades gracefully.
    """

    async def run(
        self,
        study: StudyRecord,
        state: PreprocessingState,
        segmentation_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run volumetry, producing measurements from segmentation masks."""
        result = state.stages.get("segmentation")
        if result is None or result.status not in (StageStatus.COMPLETE,):
            # Segmentation did not complete — volumetry has nothing to measure.
            state.stages["volumetry"] = StageResult(
                status=StageStatus.SKIPPED,
                reason="segmentation not complete",
            )
            state.state = state.aggregate()
            return {}
        if segmentation_result is None:
            state.stages["volumetry"] = StageResult(
                status=StageStatus.SKIPPED,
                reason="no segmentation masks",
            )
            state.state = state.aggregate()
            return {}
        # Deterministic stub: empty measurements (real impl computes volumes).
        state.stages["volumetry"] = StageResult(status=StageStatus.COMPLETE)
        state.state = state.aggregate()
        return {"study_id": study.study_id, "measurements": []}


__all__ = ["VolumetryHandler", "VolumetryUnavailable"]
