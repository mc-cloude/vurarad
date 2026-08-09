"""Priors — prior-study retrieval + registration (§3.21, §7.10.2).

The priors stage fetches prior studies for comparison and computes
``PRIOR_COMPARISON_CHANGE`` findings.  It is one of the two own-segmentation
categories (the other is ``ANATOMICAL_MEASUREMENT`` from the segmentation stage).

Per-study authz is shared with ``prior_study_service`` (WP9); for WP12 this is a
deterministic stub that reads ``StudyRecord.prior_studies`` and produces empty
comparison-change findings.
"""

from __future__ import annotations

import logging

from app.models.preprocessing import PreprocessingState, StageResult, StageStatus
from app.models.study import PriorStudyRef, StudyRecord

logger = logging.getLogger("vurarad.preprocessing.priors")


class PriorsHandler:
    """Retrieve priors and compute prior-comparison-change findings.

    In dev/CI this is a deterministic stub.  The real implementation would fetch
    prior study pixels, register them, and compute change detections.
    """

    async def run(self, study: StudyRecord, state: PreprocessingState) -> dict[str, object]:
        """Run the priors stage, producing prior-comparison findings."""
        prior_refs: list[PriorStudyRef] = study.prior_studies
        if not prior_refs:
            state.stages["priors"] = StageResult(
                status=StageStatus.COMPLETE,
                detail="no prior studies",
            )
            state.state = state.aggregate()
            return {"study_id": study.study_id, "priors": [], "findings": []}
        # Deterministic stub: produce empty comparison-change findings.
        state.stages["priors"] = StageResult(status=StageStatus.COMPLETE)
        state.state = state.aggregate()
        return {
            "study_id": study.study_id,
            "priors": [p.model_dump() for p in prior_refs],
            "findings": [],
        }


__all__ = ["PriorsHandler", "PriorStudyRef"]
