"""Study read-side models — worklist, search, detail, geometry, access URLs.

The worklist is a capped single-read queue (§3.3); search is an explicit indexed
query (§3.4); detail composes the study document with series geometry (§3.5);
access URLs are chunked V4 signed URLs (§3.6).  No response model in this module
carries ``patientName``, ``patientBirthDate``, or ``mrn`` — those fields exist
only on :class:`PatientIdentity`, which is returned by exactly one route
(``GET /studies/{studyId}/patient-identity``).
"""

from __future__ import annotations

from enum import StrEnum

from app.models.common import CamelModel
from app.models.series import StackOrderBasis, StackOrderConfidence


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class StudyStatus(StrEnum):
    """Lifecycle of a study on the worklist."""

    UNREAD = "UNREAD"
    IN_PROGRESS = "IN_PROGRESS"
    REPORTED = "REPORTED"
    SIGNED = "SIGNED"


class StudyPriority(StrEnum):
    """Clinician-set or order-derived priority — never AI-derived."""

    ROUTINE = "ROUTINE"
    URGENT = "URGENT"
    STAT = "STAT"


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------
class AssignedTo(CamelModel):
    """The radiologist a study is assigned to (de-identified)."""

    uid: str
    operator_id: str = ""
    display_name: str = ""


class PriorStudyRef(CamelModel):
    """A denormalised prior-study reference (max 5, most recent first)."""

    study_id: str
    study_date: str = ""
    modality: str = ""
    body_part: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# Worklist (§3.3)
# ---------------------------------------------------------------------------
class WorklistRow(CamelModel):
    """One study summary in the capped worklist queue.

    Carries **no patient name, no DOB, no MRN** — only ``patientRef`` and
    ``patientAgeSex``, the server-formatted de-identified strings.
    """

    study_id: str
    patient_key: str = ""
    patient_ref: str = ""
    patient_age_sex: str = ""
    accession: str = ""
    modality: str = ""
    body_part: str = ""
    description: str = ""
    study_date: str = ""
    priority: StudyPriority = StudyPriority.ROUTINE
    status: StudyStatus = StudyStatus.UNREAD
    assigned_to: AssignedTo | None = None
    series_count: int = 0
    instance_count: int = 0
    study_bytes: int = 0
    has_report: bool = False
    report_id: str | None = None
    signed_at: str | None = None
    updated_at: str = ""


class WorklistEnvelope(CamelModel):
    """The capped worklist response — 1 Firestore read, no cursor paging."""

    studies: list[WorklistRow]
    truncated: bool
    cap: int
    total_known: int
    generated_at: str
    oldest_study_date: str = ""


# ---------------------------------------------------------------------------
# Search (§3.4)
# ---------------------------------------------------------------------------
class SearchResult(CamelModel):
    """Cursor-paginated search result with observable read cost."""

    items: list[WorklistRow]
    next_cursor: str | None = None
    query_cost_reads: int = 0


# ---------------------------------------------------------------------------
# Study detail (§3.5)
# ---------------------------------------------------------------------------
class StudyDetail(CamelModel):
    """Full study detail — de-identified, no patient name/DOB/MRN.

    ``patientName``, ``patientBirthDate``, and ``mrn`` are deliberately absent;
    they are released only through :class:`PatientIdentity`.
    """

    study_id: str
    patient_key: str = ""
    patient_ref: str = ""
    patient_age_sex: str = ""
    patient_sex: str = ""
    accession: str = ""
    modality: str = ""
    body_part: str = ""
    description: str = ""
    study_date: str = ""
    referring_physician: str = ""
    clinical_history: str = ""
    status: StudyStatus = StudyStatus.UNREAD
    priority: StudyPriority = StudyPriority.ROUTINE
    assigned_to: AssignedTo | None = None
    series_count: int = 0
    instance_count: int = 0
    study_bytes: int = 0
    report_id: str | None = None
    prior_studies: list[PriorStudyRef] = []
    created_at: str = ""
    updated_at: str = ""
    version: int = 1


# ---------------------------------------------------------------------------
# Series + geometry (§3.5)
# ---------------------------------------------------------------------------
class InstanceGeometry(CamelModel):
    """One instance with spatial geometry — server-computed ``stackIndex``.

    ``imagePositionPatient``, ``imageOrientationPatient``, ``sliceLocation``,
    ``numberOfFrames``, ``stackIndex`` are the geometry contract ``[B6]``.
    """

    instance_uid: str = ""
    sop_instance_uid: str
    stack_index: int
    instance_number: int | None = None
    object_path: str = ""
    size_bytes: int = 0
    image_position_patient: list[float] | None = None
    image_orientation_patient: list[float] | None = None
    slice_location: float | None = None
    number_of_frames: int = 1
    window_center: float | None = None
    window_width: float | None = None
    rescale_slope: float | None = None
    rescale_intercept: float | None = None


class SeriesSummary(CamelModel):
    """A series summary with geometry and embedded instances."""

    series_uid: str
    series_number: int = 0
    modality: str = ""
    series_description: str = ""
    body_part: str = ""
    instance_count: int = 0
    frame_count: int = 0
    is_multi_frame: bool = False
    rows: int = 0
    columns: int = 0
    pixel_spacing: list[float] | None = None
    slice_thickness_mm: float | None = None
    spacing_between_slices_mm: float | None = None
    series_bytes: int = 0
    stack_order_basis: StackOrderBasis = StackOrderBasis.INSTANCE_NUMBER
    stack_axis: list[float] | None = None
    stack_order_confidence: StackOrderConfidence = StackOrderConfidence.UNVERIFIED
    instances: list[InstanceGeometry] = []


class SeriesListResponse(CamelModel):
    """The series listing response for ``GET /studies/{studyId}/series``."""

    study_id: str
    series: list[SeriesSummary]


# ---------------------------------------------------------------------------
# Access URLs (§3.6)
# ---------------------------------------------------------------------------
class AccessUrlEntry(CamelModel):
    """One signed URL entry in a chunk."""

    sop_instance_uid: str
    stack_index: int
    instance_number: int | None = None
    size_bytes: int = 0
    number_of_frames: int = 1
    url: str


class AccessUrlChunk(CamelModel):
    """A chunked signed-URL response.

    ``nextFromStackIndex`` is ``null`` when the end of the series is reached.
    """

    study_id: str
    series_uid: str
    expires_at: str
    from_stack_index: int
    count: int
    next_from_stack_index: int | None = None
    series_instance_count: int
    instances: list[AccessUrlEntry]


# ---------------------------------------------------------------------------
# Patient identity (§3.5) — the ONLY model carrying a patient name
# ---------------------------------------------------------------------------
class PatientIdentity(CamelModel):
    """Patient identity — returned by exactly one route.

    This is the only response model in the entire API surface that carries
    ``patientName``, ``patientBirthDate``, or ``mrn``.
    """

    patient_name: str
    patient_birth_date: str
    mrn: str


# ---------------------------------------------------------------------------
# Internal record — the Firestore study document (not a wire model)
# ---------------------------------------------------------------------------
class StudyRecord(CamelModel):
    """The full study document at ``studies/{studyId}``.

    This is the internal representation used by the service and access-policy
    layers.  It carries PHI (``patient_name``, ``patient_birth_date``, ``mrn``)
    that is never serialised into any response except :class:`PatientIdentity`.
    """

    study_id: str
    patient_key: str = ""
    patient_ref: str = ""
    patient_age_sex: str = ""
    patient_sex: str = ""
    patient_name: str = ""
    patient_birth_date: str = ""
    mrn: str = ""
    accession: str = ""
    modality: str = ""
    body_part: str = ""
    description: str = ""
    study_date: str = ""
    referring_physician: str = ""
    clinical_history: str = ""
    status: StudyStatus = StudyStatus.UNREAD
    priority: StudyPriority = StudyPriority.ROUTINE
    assigned_to: AssignedTo | None = None
    series_count: int = 0
    instance_count: int = 0
    study_bytes: int = 0
    has_report: bool = False
    report_id: str | None = None
    signed_at: str | None = None
    prior_studies: list[PriorStudyRef] = []
    series_ids: list[str] = []
    tenant_id: str = "default"
    created_at: str = ""
    updated_at: str = ""
    version: int = 1


class ViewerScope(CamelModel):
    """A viewer's data-access scope — deny-by-default, empty unless set.

    ``study_ids`` and ``referring_physicians`` are the two membership sets a
    viewer must match to read a ``SIGNED`` study (§6.3.3).
    """

    study_ids: set[str] = set()
    referring_physicians: set[str] = set()
