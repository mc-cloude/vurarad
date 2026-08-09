"""Pre-processing state — per-stage status and degradation reasons (§3.15.1).

Pre-processing is **asynchronous and non-blocking**: a study is openable the
moment its pixels are ingested, and ``preprocessingState`` reports per-stage
progress.  A stage that fails or is unavailable degrades the findings panel to
an empty state with a reason (``unavailableReasons``) — it never blocks reading,
reporting, or signing (acceptance criterion 6).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from app.models.common import CamelModel

# ---------------------------------------------------------------------------
# Stage names — the ordered pipeline (§3.21, §7.10.2)
# ---------------------------------------------------------------------------
PreprocessingStageName = Literal[
    "segmentation",
    "volumetry",
    "priors",
]

PreprocessingStateLiteral = Literal[
    "PENDING",
    "RUNNING",
    "COMPLETE",
    "PARTIAL",
    "UNAVAILABLE",
]


class StageStatus(StrEnum):
    """Status of one pre-processing stage."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    SKIPPED = "SKIPPED"


class StageResult(CamelModel):
    """Per-stage result with status, timestamps, and failure detail."""

    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None  # short reason on failure
    reason: str | None = None  # unavailable reason (NO_MODEL_AVAILABLE, etc.)
    detail: str | None = None  # human-readable detail


class PreprocessingState(CamelModel):
    """Aggregate pre-processing state for a study.

    ``state`` is the aggregate: ``PENDING`` if no stage has completed,
    ``COMPLETE`` if all stages completed, ``PARTIAL`` if some completed and some
    failed/unavailable, ``UNAVAILABLE`` if all stages are unavailable.
    """

    study_id: str
    state: PreprocessingStateLiteral = "PENDING"
    stages: dict[str, StageResult] = {}
    updated_at: datetime | None = None

    def aggregate(self) -> PreprocessingStateLiteral:
        """Compute the aggregate state from per-stage statuses."""
        if not self.stages:
            return "PENDING"
        statuses = list(self.stages.values())
        all_complete = all(s.status == StageStatus.COMPLETE for s in statuses)
        if all_complete:
            return "COMPLETE"
        all_unavailable = all(
            s.status in (StageStatus.UNAVAILABLE, StageStatus.SKIPPED) for s in statuses
        )
        if all_unavailable:
            return "UNAVAILABLE"
        any_running = any(s.status == StageStatus.RUNNING for s in statuses)
        if any_running:
            return "RUNNING"
        return "PARTIAL"


__all__ = [
    "PreprocessingStageName",
    "PreprocessingState",
    "PreprocessingStateLiteral",
    "StageResult",
    "StageStatus",
]
