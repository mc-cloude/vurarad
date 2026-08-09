"""Series and Instance geometry models — server-computed stack order ``[B6]``.

``stackIndex`` is computed once, at ingest, from the spatial geometry and stored
on the instance.  The client renders in ``stackIndex`` order and never re-derives
it: ``instanceNumber`` is an optional, occasionally duplicated DICOM attribute
that resets per acquisition on some scanners, so ordering a stack by it can
silently present slices in the wrong anatomical sequence — a clinical-safety
defect, not a cosmetic one.
"""

from __future__ import annotations

from enum import StrEnum

from app.models.common import CamelModel


class StackOrderBasis(StrEnum):
    """How ``stackIndex`` was derived."""

    IMAGE_POSITION_PATIENT_PROJECTED = "IMAGE_POSITION_PATIENT_PROJECTED"
    SLICE_LOCATION = "SLICE_LOCATION"
    FRAME_INDEX = "FRAME_INDEX"
    INSTANCE_NUMBER = "INSTANCE_NUMBER"


class StackOrderConfidence(StrEnum):
    """Confidence in the geometric ordering.

    ``RELIABLE`` — projected positions are monotonic and evenly spaced within 5%.
    ``IRREGULAR_SPACING`` — monotonic but unevenly spaced.
    ``UNVERIFIED`` — the basis is ``INSTANCE_NUMBER`` and geometry is absent.
    Anything other than ``RELIABLE`` is surfaced to the client and never
    swallowed, because a radiologist must know the stack may not be geometrically
    ordered before measuring on it.
    """

    RELIABLE = "RELIABLE"
    IRREGULAR_SPACING = "IRREGULAR_SPACING"
    UNVERIFIED = "UNVERIFIED"


class Instance(CamelModel):
    """One DICOM instance — a single SOP instance object.

    ``stackIndex`` is dense, gapless, zero-based and assigned at ingest from the
    series geometry.  ``objectPath`` is a GCS object path (not a URL) and is safe
    to cache in the client because it is useless without a signed URL.
    """

    sop_instance_uid: str
    stack_index: int
    instance_number: int | None = None
    number_of_frames: int = 1
    image_position_patient: list[float] | None = None
    image_orientation_patient: list[float] | None = None
    slice_location: float | None = None
    pixel_spacing: list[float] | None = None
    spacing_between_slices_mm: float | None = None
    window_center: float | None = None
    window_width: float | None = None
    rescale_slope: float | None = None
    rescale_intercept: float | None = None
    size_bytes: int = 0
    object_path: str = ""


class Series(CamelModel):
    """A DICOM series — instances sharing a ``SeriesInstanceUID``.

    A series with more than ``MAX_INSTANCES_PER_DOC`` instances is split into
    sibling part documents by :class:`SeriesRepository`; ``stackIndex`` is dense
    and continues across parts, so no acquisition is rejected for being large.
    The in-memory ``Series`` always holds the full instance list — splitting is a
    storage concern, not a model concern.
    """

    series_id: str
    study_id: str
    study_instance_uid: str
    series_instance_uid: str
    modality: str
    sop_class_uid: str
    stack_order_basis: StackOrderBasis
    stack_order_confidence: StackOrderConfidence
    instance_count: int
    frame_count: int
    is_multi_frame: bool = False
    instances: list[Instance]
    part_index: int = 0
    part_count: int = 1
    created_at: str = ""
