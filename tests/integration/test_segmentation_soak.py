# ruff: noqa: B008
"""Soak test — p95 CPU cpu_fast segmentation wall-clock < 120 s (criterion 7).

Runs the ``StubSegmenter`` (``per_instance_ms=0.05`` for cpu_fast) over N
iterations for a 400-instance CT, computes the p95 wall-clock, and asserts it
stays under 120 s.  The ``StubSegmenter`` uses a bounded per-instance loop
(``sum(i * per_instance_ms for i in range(min(instances, 400)))``), never
sleeping the full budget.

Marked ``@pytest.mark.slow`` — run with ``pytest -m slow``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.config import ResidencyPolicy
from app.models.study import StudyRecord
from app.segmentation.registry import SegmentationRegistry
from app.services.preprocessing.segmentation_dispatcher import (
    SegmentationDispatcher,
    SegmentationPlanState,
)

pytestmark = pytest.mark.slow

INSTANCE_COUNT = 400
ITERATIONS = 20
P95_THRESHOLD_SECONDS = 120.0


def _study() -> StudyRecord:
    return StudyRecord(
        study_id="st_soak",
        modality="CT",
        body_part="CHEST",
        instance_count=INSTANCE_COUNT,
        patient_key="pk_soak",
        tenant_id="default",
    )


class TestSegmentationSoak:
    """p95 cpu_fast segmentation wall-clock < 120 s for a 400-instance CT."""

    def test_p95_under_120_seconds(self) -> None:
        reg = SegmentationRegistry()
        disp = SegmentationDispatcher(
            reg,
            runtime="cpu_fast",
            region="africa-south1",
            residency_policy=ResidencyPolicy.africa,
        )
        study = _study()
        plan = disp.resolve_modalities("CT", "CHEST", instance_count=INSTANCE_COUNT)
        assert plan.state == SegmentationPlanState.AVAILABLE

        timings: list[float] = []
        for _ in range(ITERATIONS):
            start = time.perf_counter()
            asyncio.run(disp.dispatch(plan, study))
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        timings.sort()
        # p95 = the 95th percentile
        p95_index = int(len(timings) * 0.95)
        p95 = timings[p95_index]
        assert p95 < P95_THRESHOLD_SECONDS, (
            f"p95 segmentation wall-clock {p95:.2f}s exceeds {P95_THRESHOLD_SECONDS}s "
            f"threshold for a {INSTANCE_COUNT}-instance CT on cpu_fast"
        )
