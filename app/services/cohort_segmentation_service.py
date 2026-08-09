"""Cohort segmentation service — MONAI dispatch, versioned masks, reviewer (WP17).

Segmentation masks are **versioned and never overwritten** (criterion 6): each
create/edit appends a new :class:`SegmentationVersion` at a versioned object
path (``deid/{cohortId}/{subjectId}/seg/v{n}.nii``) and records the editor
(reviewer capture).  An existing version is never mutated — the versions list is
append-only and ``currentVersion`` is bumped.

The MONAI dispatch is the :class:`ResearchSegmenter` seam; the
:class:`StubResearchSegmenter` is the dev/CI implementation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import NotFoundError
from app.models.cohort import (
    CohortSegmentation,
    CohortSubject,
    SegmentationSource,
    SegmentationVersion,
)
from app.repositories.cohort_repo import CohortRepository
from app.services.audit_service import AuditService
from app.storage.base import ObjectStore

logger = logging.getLogger("vurarad.cohort_segmentation")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Segmenter seam — MONAI dispatch
# ---------------------------------------------------------------------------
class SegmentationOutput:
    """The result of a MONAI segmentation dispatch (mask bytes + provenance)."""

    __slots__ = ("mask_bytes", "bundle_id", "categories")

    def __init__(self, mask_bytes: bytes, bundle_id: str, categories: list[str]) -> None:
        self.mask_bytes = mask_bytes
        self.bundle_id = bundle_id
        self.categories = list(categories)


class ResearchSegmenter(Protocol):
    """The MONAI segmentation interface for a de-identified cohort subject."""

    async def segment(
        self,
        subject: CohortSubject,
        *,
        bundle_id: str,
    ) -> SegmentationOutput:
        """Produce a segmentation mask for ``subject``."""
        ...


class StubResearchSegmenter:
    """Deterministic, dependency-free MONAI segmenter for dev/CI."""

    def __init__(self, *, bundle_id: str = "monai-lung-v1") -> None:
        self._bundle_id = bundle_id

    async def segment(
        self,
        subject: CohortSubject,
        *,
        bundle_id: str,
    ) -> SegmentationOutput:
        # Deterministic placeholder mask bytes derived from the subject id.
        mask = subject.subject_id.encode() + b"\x00" * 8
        return SegmentationOutput(
            mask_bytes=mask,
            bundle_id=bundle_id or self._bundle_id,
            categories=["LUNG"],
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class CohortSegmentationService:
    """Versioned segmentation with MONAI dispatch and reviewer capture."""

    def __init__(
        self,
        cohort_repo: CohortRepository,
        segmenter: ResearchSegmenter,
        audit: AuditService,
        object_store: ObjectStore | None = None,
        *,
        deid_bucket: str = "deid",
    ) -> None:
        self._repo = cohort_repo
        self._segmenter = segmenter
        self._audit = audit
        self._object_store = object_store
        self._deid_bucket = deid_bucket

    # -- create / edit (route 63) -------------------------------------------
    async def create_or_edit_segmentation(
        self,
        user: AuthenticatedUser,
        cohort_id: str,
        subject_id: str,
        *,
        source: SegmentationSource,
        bundle_id: str = "",
    ) -> CohortSegmentation:
        """Create or extend a subject's versioned segmentation.

        Always appends a new version — an existing version is never overwritten.
        """
        subject = await self._require_subject(subject_id, cohort_id)

        existing = await self._repo.find_segmentation_for_subject(subject_id)
        if existing is None:
            segmentation_id = f"sg_{ULID()}"
            current_version = 0
            created_at = _now()
            versions: list[SegmentationVersion] = []
        else:
            segmentation_id = existing.segmentation_id
            current_version = existing.current_version
            created_at = existing.created_at
            versions = list(existing.versions)

        next_version = current_version + 1
        mask_object_path = (
            f"{self._deid_bucket}/{cohort_id}/{subject_id}/seg/v{next_version}.nii"
        )

        # MONAI dispatch — produce the mask, write it to a versioned path.
        if source == SegmentationSource.MONAI:
            output = await self._segmenter.segment(subject, bundle_id=bundle_id)
            mask_bytes = output.mask_bytes
            bundle_id = output.bundle_id
        else:
            # MANUAL_EDIT — the mask is uploaded out-of-band; record the version.
            mask_bytes = b""

        if self._object_store is not None and mask_bytes:
            await self._object_store.put(
                mask_object_path,
                mask_bytes,
                "application/octet-stream",
            )

        now = _now()
        version = SegmentationVersion(
            version=next_version,
            mask_object_path=mask_object_path,
            editor=user.operator_id or user.uid,
            editor_display_name=user.display_name or "",
            source=source,
            bundle_id=bundle_id,
            created_at=now,
        )
        # Append-only — never mutate an existing version.
        versions.append(version)

        segmentation = CohortSegmentation(
            segmentation_id=segmentation_id,
            cohort_id=cohort_id,
            subject_id=subject_id,
            current_version=next_version,
            versions=versions,
            created_at=created_at,
            updated_at=now,
        )
        await self._repo.upsert_segmentation(segmentation)
        await self._audit.record(
            event_type="COHORT_SEGMENTATION_VERSIONED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "cohortId": cohort_id,
                "subjectId": subject_id,
                "version": next_version,
                "editor": version.editor,
                "source": source.value,
            },
        )
        return segmentation

    # -- read ----------------------------------------------------------------
    async def get_segmentation(self, cohort_id: str, subject_id: str) -> CohortSegmentation:
        seg = await self._repo.find_segmentation_for_subject(subject_id)
        if seg is None or seg.cohort_id != cohort_id:
            raise NotFoundError(
                f"Segmentation for subject {subject_id} not found in cohort {cohort_id}"
            )
        return seg

    # -- helpers -------------------------------------------------------------
    async def _require_subject(self, subject_id: str, cohort_id: str) -> CohortSubject:
        subject = await self._repo.get_subject(subject_id)
        if subject is None or subject.cohort_id != cohort_id:
            raise NotFoundError(f"Subject {subject_id} not found in cohort {cohort_id}")
        return subject


__all__ = [
    "CohortSegmentationService",
    "ResearchSegmenter",
    "SegmentationOutput",
    "StubResearchSegmenter",
]
