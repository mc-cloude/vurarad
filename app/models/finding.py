"""The one finding model — both internal and external sources feed it (§3.15.1).

A ``Finding`` is the neutral, dispositionable unit that the radiologist sees and
acts on.  Two sources produce findings:

- **Our own segmentation** (``VURARAD_SEGMENTATION``): anatomical measurements
  and prior-comparison changes.  Always ``regulatoryClass == "MEASUREMENT"``;
  ``clinicalUseAllowed == True``.
- **External cleared-AI** (``EXTERNAL_CLEARED_AI``): vendor detections.  Always
  ``regulatoryClass == "CLEARED_DEVICE"`` when the adapter supplies an FDA
  K-number or CE mark; otherwise ``RUO`` with ``clinicalUseAllowed == False``.

Invariants enforced by model validators (each has a test):

1. ``regulatoryClass == "RUO"`` ⇒ ``clinicalUseAllowed == False`` AND a non-null
   ``ruoLabel`` (§5.12).
2. ``category == "EXTERNAL_DETECTION"`` requires ``source ==
   "EXTERNAL_CLEARED_AI"`` and a non-null ``fdaKNumber`` **or** ``ceMarkRef``.
3. Our own segmentation never produces ``EXTERNAL_DETECTION`` (enforced in the
   service layer; the category set is ``{ANATOMICAL_MEASUREMENT,
   PRIOR_COMPARISON_CHANGE}``).
4. No finding carries a suspicion score, urgency, triage flag, ``abnormal``
   boolean, or malignancy — those are CADt fields and storing them would make us
   the CADt device (§3.15.3).  ``test_no_cadt_fields.py`` greps for them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from app.models.common import CamelModel

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
FindingCategory = Literal[
    "ANATOMICAL_MEASUREMENT",
    "EXTERNAL_DETECTION",
    "PRIOR_COMPARISON_CHANGE",
    "RESEARCH_ONLY",
]

RegulatoryClass = Literal["RUO", "CLEARED_DEVICE", "MEASUREMENT"]

FindingSource = Literal[
    "VURARAD_SEGMENTATION",
    "EXTERNAL_CLEARED_AI",
    "PRIOR_COMPARISON",
    "RESEARCH_PIPELINE",
]

SegmentationRuntime = Literal["cpu_fast", "gpu_l4", "onprem_gpu", "external"]

DispositionState = Literal["PENDING", "CONFIRMED", "REJECTED", "EDITED"]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------
class Measurement(CamelModel):
    """One measurement on a finding — value, unit, method, optional ROI ref."""

    name: str = ""
    value: float
    unit: str
    method: str = ""
    roi_ref: str | None = None  # object key for the mask/contour


class FindingGeometry(CamelModel):
    """Spatial reference for a finding — bbox, mask object key, or point."""

    bbox: list[float] | None = None  # [x0, y0, x1, y1, z0, z1]
    mask_object_key: str | None = None
    point: list[float] | None = None  # [x, y, z]


class FindingProvenance(CamelModel):
    """Provenance for a finding — who/what produced it and under what class.

    ``weightsHash`` is ours only (sha256 of the weights blob).  External sources
    carry ``vendorName`` + ``fdaKNumber`` / ``ceMarkRef`` instead.
    """

    source: FindingSource
    producer: str
    model_version: str
    weights_hash: str | None = None
    vendor_name: str | None = None
    fda_k_number: str | None = None
    ce_mark_ref: str | None = None
    runtime: SegmentationRuntime
    produced_at: datetime
    ruo_label: str | None = None  # required non-null when regulatoryClass == RUO
    ingest_ref: str | None = None  # findings_ingest/{ingestId} for external


class EvidenceRef(CamelModel):
    """A reference to evidence linked after confirmation (§3.16, WP13)."""

    evidence_id: str
    rule_id: str = ""
    citation_id: str = ""


class Disposition(CamelModel):
    """Disposition state — PENDING until the radiologist acts (§3.15.2)."""

    state: DispositionState = "PENDING"
    by_uid: str | None = None
    by_operator_id: str | None = None
    at: datetime | None = None
    dictation_ref: str | None = None
    confirmed_text: str | None = None  # the ONLY text that may enter a report


class Finding(CamelModel):
    """The neutral, dispositionable finding unit (§3.15.1)."""

    finding_id: str  # fd_<ulid>
    study_id: str
    series_uid: str | None = None
    sop_instance_uids: list[str] = []
    category: FindingCategory
    label: str = ""
    body_site: str | None = None
    measurements: list[Measurement] = []
    geometry: FindingGeometry | None = None
    provenance: FindingProvenance
    regulatory_class: RegulatoryClass
    clinical_use_allowed: bool
    disposition: Disposition = Disposition()
    evidence: list[EvidenceRef] = []

    # -- invariant 1: RUO ⇒ clinicalUseAllowed == False AND ruoLabel non-null --
    @model_validator(mode="after")
    def _ruo_implies_no_clinical_use(self) -> Finding:
        if self.regulatory_class == "RUO":
            if self.clinical_use_allowed:
                raise PydanticCustomError(
                    "ruo_clinical_use",
                    "A Finding with regulatoryClass == 'RUO' must have clinicalUseAllowed == False",
                )
            if not self.provenance.ruo_label:
                raise PydanticCustomError(
                    "ruo_label_required",
                    "A Finding with regulatoryClass == 'RUO' must have a "
                    "non-null provenance.ruoLabel",
                )
        return self

    # -- invariant 2: EXTERNAL_DETECTION requires external source + clearance --
    @model_validator(mode="after")
    def _external_detection_requires_clearance(self) -> Finding:
        if self.category == "EXTERNAL_DETECTION":
            if self.provenance.source != "EXTERNAL_CLEARED_AI":
                raise PydanticCustomError(
                    "external_detection_source",
                    "category == 'EXTERNAL_DETECTION' requires "
                    "provenance.source == 'EXTERNAL_CLEARED_AI'",
                )
            if not self.provenance.fda_k_number and not self.provenance.ce_mark_ref:
                raise PydanticCustomError(
                    "external_detection_clearance",
                    "category == 'EXTERNAL_DETECTION' requires a non-null "
                    "provenance.fdaKNumber or provenance.ceMarkRef",
                )
        return self


# ---------------------------------------------------------------------------
# Wire models for disposition (§3.15.2)
# ---------------------------------------------------------------------------
class DispositionRequest(CamelModel):
    """Request body for ``POST /findings/{id}/disposition``."""

    state: DispositionState
    confirmed_text: str | None = None
    dictation_ref: str | None = None

    @model_validator(mode="after")
    def _require_confirmed_text(self) -> DispositionRequest:
        """``confirmedText`` is required for CONFIRMED and EDITED (§3.15.2)."""
        if self.state in ("CONFIRMED", "EDITED") and not self.confirmed_text:
            raise PydanticCustomError(
                "confirmed_text_required",
                "confirmedText is required for state CONFIRMED or EDITED",
            )
        return self


# ---------------------------------------------------------------------------
# Response envelope (§3.15.1)
# ---------------------------------------------------------------------------
class UnavailableReason(CamelModel):
    """A reason a capability is unavailable for a study."""

    capability: str
    reason: str
    detail: str = ""


class FindingsResponse(CamelModel):
    """Response for ``GET /studies/{studyId}/findings`` (§3.15.1)."""

    study_id: str
    generated_at: datetime
    preprocessing_state: str = "COMPLETE"
    findings: list[Finding] = []
    unavailable_reasons: list[UnavailableReason] = []


__all__ = [
    "Disposition",
    "DispositionRequest",
    "DispositionState",
    "EvidenceRef",
    "Finding",
    "FindingCategory",
    "FindingGeometry",
    "FindingProvenance",
    "FindingSource",
    "FindingsResponse",
    "Measurement",
    "RegulatoryClass",
    "SegmentationRuntime",
    "UnavailableReason",
]
