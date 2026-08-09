"""Review queue — route uncertain regions to human review.

The pipeline routes every region the :class:`~app.services.deid.decision.Decider`
resolves to ``REVIEW`` here: low-confidence classifications, forced-modality
regions, unvalidated-source regions, classifier errors, and OCR text with no
classification.  Each becomes a :class:`DeidReview` persisted in
``deid_reviews`` for a reviewer holding the ``deid:review`` capability.

Fail-closed contract: a REVIEW region is **redacted** in the pixel data (no PHI
leaks) *and* enqueued — the reviewer can restore a wrongly-redacted clinical
annotation (status ``RESTORED``) or confirm the redaction (``APPROVED``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.repositories.deid_repo import (
    DeidReview,
    DeidReviewRepository,
    ReviewStatus,
    bbox_to_list,
)
from app.services.deid.ocr import BBox


@dataclass(frozen=True, slots=True)
class ReviewableRegion:
    """A region the decision layer routed to review."""

    bbox: BBox
    text: str
    label: str
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewBuildContext:
    """Identifies the run/series/instance a reviewable region belongs to."""

    run_id: str
    study_id: str
    series_id: str
    sop_instance_uid: str
    modality: str
    manufacturer: str


class ReviewQueue:
    """Build, persist, and transition de-ID review items."""

    def __init__(self, repo: DeidReviewRepository) -> None:
        self._repo = repo

    def build_items(
        self,
        ctx: ReviewBuildContext,
        regions: list[ReviewableRegion],
    ) -> list[DeidReview]:
        now = time.time()
        return [
            DeidReview(
                review_id=_review_id(ctx.run_id, idx, region.bbox),
                run_id=ctx.run_id,
                study_id=ctx.study_id,
                series_id=ctx.series_id,
                sop_instance_uid=ctx.sop_instance_uid,
                modality=ctx.modality,
                manufacturer=ctx.manufacturer,
                bbox=bbox_to_list(region.bbox),
                text=region.text,
                label=region.label,
                confidence=region.confidence,
                reason=region.reason,
                status=ReviewStatus.PENDING,
                created_at=now,
            )
            for idx, region in enumerate(regions)
        ]

    async def enqueue(self, items: list[DeidReview]) -> int:
        return await self._repo.create_many(items)

    async def list_pending(self, limit: int = 100) -> list[DeidReview]:
        return await self._repo.list_pending(limit)

    async def list_by_run(self, run_id: str, limit: int = 1000) -> list[DeidReview]:
        return await self._repo.list_by_run(run_id, limit)

    async def resolve(
        self,
        review_id: str,
        status: ReviewStatus,
        actor: str,
        note: str = "",
    ) -> DeidReview:
        return await self._repo.resolve(review_id, status, actor, note)


def _review_id(run_id: str, idx: int, bbox: BBox) -> str:
    import hashlib

    raw = f"{run_id}:{idx}:{bbox.x0}:{bbox.y0}:{bbox.x1}:{bbox.y1}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]
