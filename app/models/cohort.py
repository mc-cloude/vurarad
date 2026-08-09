"""Cohort workbench models — de-identified research artifacts (WP17).

Every model in this module is a **response model** for the research workbench
and is therefore subject to the research/clinical barrier: **no field may hold
a ``studyId`` or ``patientKey``**.  The pseudonym→patient mapping lives only in
the write-restricted ``deid_links`` collection (read-gated by ``patient:erase``),
never on a cohort subject.  A static test
(``test_research_clinical_barrier.py``) asserts neither snake_case nor the
camelCase wire alias appears on any class defined here.

The cohort surface is RUO (research use only) — ``regulatoryClass == "RUO"``
on every response (§5.12 / D9).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from app.models.common import CamelModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class CohortStatus(StrEnum):
    """Lifecycle of a cohort."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    RETENTION_EXPIRED = "RETENTION_EXPIRED"


class CohortSubjectStatus(StrEnum):
    """Lifecycle of a cohort subject.

    ``PENDING_REVIEW`` is the initial state when the de-ID pass left open review
    items; a subject cannot become ``ACTIVE`` until every item is resolved
    (criterion 3).  ``REMOVED`` is the erasure terminal state.
    """

    PENDING_REVIEW = "PENDING_REVIEW"
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class SegmentationSource(StrEnum):
    """Provenance of a segmentation mask version."""

    MONAI = "MONAI"
    MANUAL_EDIT = "MANUAL_EDIT"


class DeidReviewStatus(StrEnum):
    """Lifecycle of a single de-ID review item."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


IrbDetermination = Literal["EXEMPT", "EXPEDITED", "FULL_BOARD", "NOT_HUMAN_SUBJECTS"]


# ---------------------------------------------------------------------------
# De-ID review item — carried by a cohort subject (criterion 3)
# ---------------------------------------------------------------------------
class DeidReviewItem(CamelModel):
    """One item flagged by the de-ID pixel/metadata pass for human review.

    A subject with any ``OPEN`` item cannot become ``ACTIVE`` and feature
    extraction against it returns ``409 DEID_REVIEW_PENDING``.
    """

    item_id: str
    category: str = "BURNED_TEXT"
    status: DeidReviewStatus = DeidReviewStatus.OPEN
    confidence: float = 0.0
    detail: str = ""


# ---------------------------------------------------------------------------
# Cohort — the IRB-backed research grouping
# ---------------------------------------------------------------------------
class Cohort(CamelModel):
    """A research cohort.

    ``irbReference`` and ``irbDetermination`` are required (criterion 7):
    de-identification reduces but does not automatically remove IRB
    obligations, so the determination is captured rather than assumed away.
    """

    cohort_id: str
    name: str
    description: str = ""
    irb_reference: str
    irb_determination: IrbDetermination
    status: CohortStatus = CohortStatus.ACTIVE
    subject_count: int = 0
    retention_days: int = 365
    regulatory_class: str = "RUO"
    created_by: str
    created_at: str
    updated_at: str
    version: int = 1

    @model_validator(mode="after")
    def _require_irb_fields(self) -> Cohort:
        if not self.irb_reference.strip():
            raise ValueError("irbReference is required for cohort creation")
        if not self.irb_determination.strip():
            raise ValueError("irbDetermination is required for cohort creation")
        return self


# ---------------------------------------------------------------------------
# Cohort subject — a de-identified subject added only via DeidPipeline
# ---------------------------------------------------------------------------
class CohortSubject(CamelModel):
    """A de-identified subject in a cohort.

    ``subject_id`` is the opaque pseudonym (``cs_…``) minted by
    :class:`DeidPipeline`; ``deid_object_path`` points at the de-identified
    pixels in the de-ID bucket.  No ``studyId`` or ``patientKey`` is stored —
    the re-identification mapping lives only in ``deid_links``.
    """

    subject_id: str
    cohort_id: str
    status: CohortSubjectStatus = CohortSubjectStatus.PENDING_REVIEW
    deid_object_path: str
    pixel_pass_passed: bool
    review_items: list[DeidReviewItem] = []
    source_kind: str = "WORKLIST"
    source_modality: str = ""
    source_body_part: str = ""
    created_at: str
    updated_at: str
    version: int = 1


# ---------------------------------------------------------------------------
# Segmentation — versioned masks, never overwritten, editor recorded
# ---------------------------------------------------------------------------
class SegmentationVersion(CamelModel):
    """One immutable mask version.

    Masks are versioned and **never overwritten** (criterion 6): each edit
    appends a new :class:`SegmentationVersion` at a versioned object path and
    records the editor (reviewer capture).
    """

    version: int
    mask_object_path: str
    editor: str
    editor_display_name: str = ""
    source: SegmentationSource
    bundle_id: str = ""
    created_at: str


class CohortSegmentation(CamelModel):
    """The versioned segmentation aggregate for a cohort subject.

    ``current_version`` points at the latest mask; ``versions`` is an
    append-only history.  Editing appends a version and bumps
    ``current_version`` — an existing version is never mutated.
    """

    segmentation_id: str
    cohort_id: str
    subject_id: str
    current_version: int = 0
    versions: list[SegmentationVersion] = []
    regulatory_class: str = "RUO"
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class CreateCohortRequest(CamelModel):
    """Body of ``POST /cohorts``."""

    name: str
    description: str = ""
    irb_reference: str
    irb_determination: IrbDetermination
    retention_days: int = 365

    @model_validator(mode="after")
    def _require_irb_fields(self) -> CreateCohortRequest:
        if not self.name.strip():
            raise ValueError("name is required")
        if not self.irb_reference.strip():
            raise ValueError("irbReference is required")
        if not self.irb_determination.strip():
            raise ValueError("irbDetermination is required")
        return self


class AddSubjectRequest(CamelModel):
    """Body of ``POST /cohorts/{cohortId}/subjects``.

    ``studyId`` is the *clinical* source to de-identify (worklist path) and is
    accepted only on the request — it never appears on the response subject.
    """

    source_kind: Literal["WORKLIST", "UPLOAD"] = "WORKLIST"
    study_id: str = ""
    upload_ref: str = ""
    modality: str = ""
    body_part: str = ""

    @model_validator(mode="after")
    def _validate_source(self) -> AddSubjectRequest:
        if self.source_kind == "WORKLIST" and not self.study_id.strip():
            raise ValueError("studyId is required when sourceKind is WORKLIST")
        if self.source_kind == "UPLOAD" and not self.upload_ref.strip():
            raise ValueError("uploadRef is required when sourceKind is UPLOAD")
        return self


class CreateSegmentationRequest(CamelModel):
    """Body of ``POST /cohorts/{cohortId}/segmentation``."""

    subject_id: str
    source: SegmentationSource = SegmentationSource.MONAI
    bundle_id: str = ""


__all__ = [
    "AddSubjectRequest",
    "Cohort",
    "CohortSegmentation",
    "CohortStatus",
    "CohortSubject",
    "CohortSubjectStatus",
    "CreateCohortRequest",
    "CreateSegmentationRequest",
    "DeidReviewItem",
    "DeidReviewStatus",
    "IrbDetermination",
    "SegmentationSource",
    "SegmentationVersion",
]
