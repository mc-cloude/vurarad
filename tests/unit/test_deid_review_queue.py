"""Unit tests for the review queue — build, enqueue, resolve."""

from __future__ import annotations

import pytest

from app.repositories.base import InMemoryDocumentStore
from app.repositories.deid_repo import DeidReviewRepository, ReviewStatus
from app.services.deid.ocr import BBox
from app.services.deid.review_queue import (
    ReviewableRegion,
    ReviewBuildContext,
    ReviewQueue,
)

_CTX = ReviewBuildContext(
    run_id="run-1",
    study_id="study-1",
    series_id="series-1",
    sop_instance_uid="1.2.3",
    modality="US",
    manufacturer="Acme",
)


def _queue() -> ReviewQueue:
    return ReviewQueue(DeidReviewRepository(InMemoryDocumentStore()))


# ---------------------------------------------------------------------------
# build_items
# ---------------------------------------------------------------------------
class TestBuildItems:
    def test_builds_one_item_per_region(self) -> None:
        q = _queue()
        regions = [
            ReviewableRegion(BBox(0, 0, 5, 5), "John Doe", "PHI", 0.5, "LOW_CONFIDENCE"),
            ReviewableRegion(BBox(1, 1, 6, 6), "LEFT", "CLINICAL", 0.3, "FORCED_MODALITY"),
        ]
        items = q.build_items(_CTX, regions)
        assert len(items) == 2
        assert all(i.status == ReviewStatus.PENDING for i in items)
        assert all(i.run_id == "run-1" for i in items)

    def test_review_id_is_deterministic(self) -> None:
        q = _queue()
        regions = [ReviewableRegion(BBox(0, 0, 5, 5), "x", "PHI", 0.5, "LOW_CONFIDENCE")]
        a = q.build_items(_CTX, regions)
        b = q.build_items(_CTX, regions)
        assert a[0].review_id == b[0].review_id

    def test_review_id_differs_by_bbox(self) -> None:
        q = _queue()
        regions = [
            ReviewableRegion(BBox(0, 0, 5, 5), "x", "PHI", 0.5, "LOW_CONFIDENCE"),
            ReviewableRegion(BBox(10, 10, 15, 15), "y", "PHI", 0.5, "LOW_CONFIDENCE"),
        ]
        items = q.build_items(_CTX, regions)
        assert items[0].review_id != items[1].review_id

    def test_bbox_serialised_to_list(self) -> None:
        q = _queue()
        regions = [ReviewableRegion(BBox(2, 3, 8, 9), "x", "PHI", 0.5, "LOW_CONFIDENCE")]
        items = q.build_items(_CTX, regions)
        assert items[0].bbox == [2, 3, 8, 9]

    def test_empty_regions_yields_no_items(self) -> None:
        assert _queue().build_items(_CTX, []) == []


# ---------------------------------------------------------------------------
# enqueue / list / resolve (async)
# ---------------------------------------------------------------------------
class TestReviewQueueAsync:
    async def test_enqueue_persists_items(self) -> None:
        q = _queue()
        regions = [
            ReviewableRegion(BBox(0, 0, 5, 5), "John Doe", "PHI", 0.5, "LOW_CONFIDENCE"),
        ]
        items = q.build_items(_CTX, regions)
        count = await q.enqueue(items)
        assert count == 1
        pending = await q.list_pending()
        assert len(pending) == 1
        assert pending[0].review_id == items[0].review_id

    async def test_list_by_run(self) -> None:
        q = _queue()
        items = q.build_items(
            _CTX,
            [ReviewableRegion(BBox(0, 0, 5, 5), "x", "PHI", 0.5, "LOW_CONFIDENCE")],
        )
        await q.enqueue(items)
        rows = await q.list_by_run("run-1")
        assert len(rows) == 1
        assert await q.list_by_run("other") == []

    async def test_resolve_transitions_status(self) -> None:
        q = _queue()
        items = q.build_items(
            _CTX,
            [ReviewableRegion(BBox(0, 0, 5, 5), "x", "PHI", 0.5, "LOW_CONFIDENCE")],
        )
        await q.enqueue(items)
        resolved = await q.resolve(items[0].review_id, ReviewStatus.APPROVED, "rev1", "ok")
        assert resolved.status == ReviewStatus.APPROVED
        assert resolved.resolved_by == "rev1"
        assert resolved.resolution_note == "ok"
        assert resolved.resolved_at is not None
        # A resolved item is no longer pending.
        assert await q.list_pending() == []

    async def test_resolve_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            await _queue().resolve("nope", ReviewStatus.APPROVED, "rev1")
