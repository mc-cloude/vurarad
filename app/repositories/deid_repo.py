"""De-identification persistence — review queue and the write-restricted link map.

Two collections, deliberately separate:

* ``deid_reviews`` — human-review items created by the pipeline for low-
  confidence, forced-modality, and unvalidated-source regions.  Created by the
  pipeline, resolved (approved / rejected / restored) by a reviewer holding the
  ``deid:review`` capability.  Mutable: status transitions are recorded.

* ``deid_links`` — the immutable mapping from a source object to its
  de-identified object.  **Write-restricted by construction**: the repository
  exposes ``create`` (atomic, fails if the link exists) and ``get`` only — there
  is no ``update`` or ``delete``.  A de-id link is a permanent provenance record;
  deleting it would orphan a de-identified object from its source.

``deid_links`` is imported by exactly :class:`~app.services.deid.pipeline.DeidPipeline`
(and, in a future WP, the erasure/cohort subject-removal service).  It is
unreachable from ``cohort:*`` capabilities: the de-ID routes are gated by the
``deid:review`` capability, never by a research/cohort capability.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import TYPE_CHECKING

from app.models.common import CamelModel
from app.repositories.base import DocumentStore

if TYPE_CHECKING:
    # BBox is used only for the ``bbox_to_list`` annotation; importing it at
    # runtime would create a cycle (app.services.deid.__init__ → pipeline →
    # deid_repo).  Annotations are lazy via ``from __future__ import annotations``.
    from app.services.deid.ocr import BBox

DEID_REVIEWS_COLLECTION: str = "deid_reviews"
DEID_LINKS_COLLECTION: str = "deid_links"  # write-restricted — no update/delete


# ---------------------------------------------------------------------------
# Review items
# ---------------------------------------------------------------------------
class ReviewStatus(StrEnum):
    """Lifecycle of a review item."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"  # redaction confirmed correct
    REJECTED = "REJECTED"  # region was PHI that should not have been redacted
    RESTORED = "RESTORED"  # clinical annotation wrongly redacted → restored


class DeidReview(CamelModel):
    """One region routed to human review."""

    review_id: str
    run_id: str
    study_id: str
    series_id: str
    sop_instance_uid: str
    modality: str
    manufacturer: str
    bbox: list[int]  # [x0, y0, x1, y1]
    text: str
    label: str
    confidence: float
    reason: str
    status: ReviewStatus = ReviewStatus.PENDING
    created_at: float
    resolved_at: float | None = None
    resolved_by: str = ""
    resolution_note: str = ""


class DeidReviewRepository:
    """Persist and transition de-ID review items."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def create(self, review: DeidReview) -> None:
        await self._store.set(DEID_REVIEWS_COLLECTION, review.review_id, review.model_dump())

    async def create_many(self, reviews: list[DeidReview]) -> int:
        count = 0
        for review in reviews:
            await self._store.set(DEID_REVIEWS_COLLECTION, review.review_id, review.model_dump())
            count += 1
        return count

    async def get(self, review_id: str) -> DeidReview | None:
        doc = await self._store.get(DEID_REVIEWS_COLLECTION, review_id)
        return DeidReview.model_validate(doc) if doc is not None else None

    async def list_pending(self, limit: int = 100) -> list[DeidReview]:
        rows = await self._store.query(
            DEID_REVIEWS_COLLECTION,
            where=[("status", "==", ReviewStatus.PENDING.value)],
            limit=limit,
        )
        return [DeidReview.model_validate(doc) for _id, doc in rows]

    async def list_by_run(self, run_id: str, limit: int = 1000) -> list[DeidReview]:
        rows = await self._store.query(
            DEID_REVIEWS_COLLECTION,
            where=[("run_id", "==", run_id)],
            limit=limit,
        )
        return [DeidReview.model_validate(doc) for _id, doc in rows]

    async def resolve(
        self,
        review_id: str,
        status: ReviewStatus,
        actor: str,
        note: str = "",
    ) -> DeidReview:
        review = await self.get(review_id)
        if review is None:
            raise ValueError(f"Deid review {review_id} not found")
        review.status = status
        review.resolved_at = time.time()
        review.resolved_by = actor
        review.resolution_note = note
        await self._store.set(DEID_REVIEWS_COLLECTION, review_id, review.model_dump())
        return review


# ---------------------------------------------------------------------------
# De-id link map — write-restricted (immutable provenance)
# ---------------------------------------------------------------------------
class DeidLink(CamelModel):
    """The permanent mapping from a source object to its de-identified object."""

    source_object_path: str
    deid_object_path: str
    run_id: str
    study_id: str
    series_id: str
    original_sop_instance_uid: str
    new_sop_instance_uid: str
    created_at: float


class DeidLinkRepository:
    """Write-restricted link map — ``create`` and ``get`` only.

    There is intentionally no ``update`` or ``delete``: a de-id link is an
    immutable provenance record.  Immutability is enforced by the absence of
    mutating methods, not by a runtime check a caller could bypass.
    """

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def create(self, link: DeidLink) -> bool:
        """Atomically create a link.  Returns ``False`` if it already exists."""
        return await self._store.create(
            DEID_LINKS_COLLECTION, link.source_object_path, link.model_dump()
        )

    async def get(self, source_object_path: str) -> DeidLink | None:
        doc = await self._store.get(DEID_LINKS_COLLECTION, source_object_path)
        return DeidLink.model_validate(doc) if doc is not None else None


def bbox_to_list(bbox: BBox) -> list[int]:
    return [bbox.x0, bbox.y0, bbox.x1, bbox.y1]
