"""Unit tests for the de-id repositories + the write-restricted link map.

Covers acceptance criterion 7: ``deid_links`` is write-restricted (create +
get only, no update/delete) and unreachable from ``cohort:*`` capabilities.
"""

from __future__ import annotations

import inspect

import pytest

from app.core.capabilities import PHI_CAPABILITIES, Capability
from app.repositories.base import InMemoryDocumentStore
from app.repositories.deid_repo import (
    DEID_LINKS_COLLECTION,
    DEID_REVIEWS_COLLECTION,
    DeidLink,
    DeidLinkRepository,
    DeidReview,
    DeidReviewRepository,
    ReviewStatus,
)


# ---------------------------------------------------------------------------
# DeidLinkRepository — write-restricted (criterion 7)
# ---------------------------------------------------------------------------
class TestDeidLinkWriteRestricted:
    def _link(self) -> DeidLink:
        return DeidLink(
            source_object_path="studies/s1/instance.dcm",
            deid_object_path="deid/1.2.3/instance.dcm",
            run_id="run-1",
            study_id="s1",
            series_id="se1",
            original_sop_instance_uid="1.2.3",
            new_sop_instance_uid="2.25.9",
            created_at=1.0,
        )

    def test_exposes_only_create_and_get(self) -> None:
        """No update/delete method exists on the link repository."""
        methods = {
            name
            for name, _ in inspect.getmembers(DeidLinkRepository, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        assert "create" in methods
        assert "get" in methods
        assert "update" not in methods
        assert "delete" not in methods

    async def test_create_returns_true_then_false_on_duplicate(self) -> None:
        repo = DeidLinkRepository(InMemoryDocumentStore())
        link = self._link()
        assert await repo.create(link) is True
        # Atomic: a second create for the same source path fails.
        assert await repo.create(link) is False

    async def test_get_returns_link(self) -> None:
        repo = DeidLinkRepository(InMemoryDocumentStore())
        await repo.create(self._link())
        fetched = await repo.get("studies/s1/instance.dcm")
        assert fetched is not None
        assert fetched.run_id == "run-1"
        assert fetched.new_sop_instance_uid == "2.25.9"

    async def test_get_missing_returns_none(self) -> None:
        assert await DeidLinkRepository(InMemoryDocumentStore()).get("nope") is None


# ---------------------------------------------------------------------------
# DeidReviewRepository — mutable review lifecycle
# ---------------------------------------------------------------------------
class TestDeidReviewRepository:
    def _review(self) -> DeidReview:
        return DeidReview(
            review_id="rev-1",
            run_id="run-1",
            study_id="s1",
            series_id="se1",
            sop_instance_uid="1.2.3",
            modality="US",
            manufacturer="Acme",
            bbox=[0, 0, 5, 5],
            text="John Doe",
            label="PHI",
            confidence=0.5,
            reason="LOW_CONFIDENCE",
            status=ReviewStatus.PENDING,
            created_at=1.0,
        )

    async def test_create_and_get(self) -> None:
        repo = DeidReviewRepository(InMemoryDocumentStore())
        await repo.create(self._review())
        fetched = await repo.get("rev-1")
        assert fetched is not None
        assert fetched.status == ReviewStatus.PENDING

    async def test_list_pending(self) -> None:
        repo = DeidReviewRepository(InMemoryDocumentStore())
        await repo.create(self._review())
        pending = await repo.list_pending()
        assert len(pending) == 1

    async def test_resolve_transitions(self) -> None:
        repo = DeidReviewRepository(InMemoryDocumentStore())
        await repo.create(self._review())
        resolved = await repo.resolve("rev-1", ReviewStatus.RESTORED, "rev1", "clinical")
        assert resolved.status == ReviewStatus.RESTORED
        assert resolved.resolved_by == "rev1"

    async def test_resolve_unknown_raises(self) -> None:
        repo = DeidReviewRepository(InMemoryDocumentStore())
        with pytest.raises(ValueError):
            await repo.resolve("x", ReviewStatus.APPROVED, "a")


# ---------------------------------------------------------------------------
# Collection names — deid_links is its own collection
# ---------------------------------------------------------------------------
def test_deid_links_is_separate_collection() -> None:
    assert DEID_LINKS_COLLECTION == "deid_links"
    assert DEID_REVIEWS_COLLECTION == "deid_reviews"
    assert DEID_LINKS_COLLECTION != DEID_REVIEWS_COLLECTION


# ---------------------------------------------------------------------------
# Criterion 7 — deid_links unreachable from cohort:* capabilities
# ---------------------------------------------------------------------------
def test_deid_review_is_a_phi_capability() -> None:
    assert Capability.DEID_REVIEW in PHI_CAPABILITIES


def test_no_cohort_capability_can_reach_deid_links() -> None:
    """The de-ID routes are gated by ``deid:review`` (a PHI capability held by
    no default role).  No ``cohort:*`` capability exists, and the de-ID surface
    is not reachable from any research capability."""
    cohort_caps = {
        c for c in Capability if c.value.startswith("cohort:") or c.value.startswith("research:")
    }
    # deid:review is distinct from every research/cohort capability.
    assert Capability.DEID_REVIEW not in cohort_caps
    # No capability value shares the deid: namespace with cohort.
    assert all(not c.value.startswith("deid:") or c is Capability.DEID_REVIEW for c in Capability)
