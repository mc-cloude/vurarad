"""AdapterFinding → Finding normalizer (§3.15.3, §3.15.4).

The single place where vendor-supplied adapter findings become the neutral,
dispositionable :class:`~app.models.finding.Finding`.  It enforces three
clearance-tract rules that keep vuraRAD out of the CADt device class:

1. **Proof-based clearance.**  ``regulatoryClass == "CLEARED_DEVICE"`` is set
   ONLY when the registry's :class:`VendorIdentity` carries an FDA K-number or
   a CE mark.  The clearance is read from the registry, never from the vendor
   payload, so the system never upgrades a finding on the vendor's behalf.  A
   vendor with no registered clearance becomes ``RUO`` +
   ``clinicalUseAllowed == False`` + ``RESEARCH_ONLY`` + auto-``REJECTED`` with
   a ``NO_CLEARANCE_REFERENCE`` marker.

2. **CADt strip.**  Suspicion, urgency, triage, and priority from vendor
   payloads are dropped here and counted in :class:`NormalizationStats` — they
   never reach :class:`Finding` (storing them would make us the CADt device).

3. **PHI redaction.**  Vendor free text passes through
   :class:`PhiRedactionFilter` before it is attached to a finding, so PHI that
   a vendor accidentally embeds in a description never reaches storage.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ulid import ULID

from app.models.finding import (
    Disposition,
    DispositionState,
    Finding,
    FindingCategory,
    FindingGeometry,
    FindingProvenance,
    Measurement,
    RegulatoryClass,
    SegmentationRuntime,
)
from app.services.findings_ingest.base import (
    AdapterFinding,
    AdapterGeometry,
    AdapterMeasurement,
    VendorIdentity,
)

__all__ = [
    "NO_CLEARANCE_RUO_LABEL",
    "NormalizedFinding",
    "NormalizationStats",
    "PhiRedactionFilter",
    "FindingNormalizer",
]


# Human-readable RUO label stamped on findings whose source lacks clearance.
NO_CLEARANCE_RUO_LABEL = (
    "External AI source has no registered FDA K-number or CE mark clearance "
    "reference — research use only, not for clinical decision-making."
)


# ---------------------------------------------------------------------------
# Telemetry accumulator
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NormalizationStats:
    """Counts emitted by the normalizer for telemetry / the ingest response."""

    cadt_fields_dropped: int = 0
    no_clearance_reference_count: int = 0

    def merge(self, other: NormalizationStats) -> NormalizationStats:
        return NormalizationStats(
            cadt_fields_dropped=self.cadt_fields_dropped + other.cadt_fields_dropped,
            no_clearance_reference_count=self.no_clearance_reference_count
            + other.no_clearance_reference_count,
        )


@dataclass(frozen=True, slots=True)
class NormalizedFinding:
    """A normalized finding plus its PHI-redacted vendor free text.

    The :class:`Finding` model is the neutral, dispositionable unit and carries
    no free-text field; the vendor's description is redacted here and stored on
    the ingest record (referenced by ``provenance.ingestRef``), never raw.
    """

    finding: Finding
    redacted_free_text: str | None


# ---------------------------------------------------------------------------
# PHI redaction for vendor free text
# ---------------------------------------------------------------------------
class PhiRedactionFilter:
    """Redact PHI from a free-text string before it is stored on a finding.

    Two mechanisms:

    - **Known PHI values** (``patient_name``, ``mrn``, ``accession``,
      ``patient_birth_date`` from the study) are redacted verbatim,
      case-insensitively, longest-first so ``"Doe, John"`` is removed before a
      shorter substring.
    - **Regex heuristics** catch PHI the vendor may have introduced without a
      matching study field: email addresses, calendar dates (DOB-shaped), and
      phone numbers.

    Over-redaction is preferred to leakage: a false positive removes a token,
    a false negative leaks PHI.
    """

    _EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    _DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
    # Conservative phone pattern: a leading digit, >=6 separator/digit chars,
    # a trailing digit.  The 'x' in "12.5 x 10.0 mm" breaks it (not in the class)
    # so measurement prose is not falsely redacted.
    _PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")

    def __init__(self, known_phi: Mapping[str, str] | None = None) -> None:
        self._known = tuple(v for v in (known_phi or {}).values() if v)

    def filter(self, text: str) -> str:
        """Return ``text`` with PHI scrubbed."""
        out = text
        for value in sorted(self._known, key=len, reverse=True):
            if value:
                out = re.sub(re.escape(value), "[REDACTED]", out, flags=re.IGNORECASE)
        out = self._EMAIL_RE.sub("[REDACTED]", out)
        out = self._DATE_RE.sub("[REDACTED_DATE]", out)
        out = self._PHONE_RE.sub("[REDACTED]", out)
        return out


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Classification:
    """The regulatory classification + disposition for one adapter finding."""

    category: FindingCategory
    regulatory_class: RegulatoryClass
    clinical_use_allowed: bool
    disposition_state: DispositionState
    no_clearance_reference: bool
    ruo_label: str | None


class FindingNormalizer:
    """Convert :class:`AdapterFinding` records into stored :class:`Finding`."""

    def __init__(self, phi_filter: PhiRedactionFilter) -> None:
        self._phi_filter = phi_filter

    # -- single finding ------------------------------------------------------
    def normalize(
        self,
        adapter_finding: AdapterFinding,
        vendor: VendorIdentity,
        study_id: str,
        ingest_ref: str,
        finding_id: str | None = None,
    ) -> tuple[NormalizedFinding, NormalizationStats]:
        """Normalize one adapter finding into a :class:`Finding`.

        Returns the normalized finding (with its PHI-redacted free text) and the
        per-finding telemetry stats (CADt fields dropped, no-clearance marker).
        """
        cadt_present = adapter_finding.cadt_fields_present()
        classification = self._classify(vendor)
        provenance = self._provenance(vendor, classification, ingest_ref)

        finding = Finding(
            finding_id=finding_id or f"fd_{ULID()}",
            study_id=study_id,
            series_uid=adapter_finding.series_instance_uid,
            sop_instance_uids=list(adapter_finding.sop_instance_uids),
            category=classification.category,
            label=adapter_finding.label,
            body_site=adapter_finding.body_site,
            measurements=[self._measurement(m) for m in adapter_finding.measurements],
            geometry=self._geometry(adapter_finding.geometry),
            provenance=provenance,
            regulatory_class=classification.regulatory_class,
            clinical_use_allowed=classification.clinical_use_allowed,
            disposition=Disposition(state=classification.disposition_state),
        )

        redacted_free_text = (
            self._phi_filter.filter(adapter_finding.free_text)
            if adapter_finding.free_text
            else None
        )

        stats = NormalizationStats(
            cadt_fields_dropped=len(cadt_present),
            no_clearance_reference_count=1 if classification.no_clearance_reference else 0,
        )
        return NormalizedFinding(finding=finding, redacted_free_text=redacted_free_text), stats

    # -- many findings -------------------------------------------------------
    def normalize_many(
        self,
        adapter_findings: list[AdapterFinding],
        vendor: VendorIdentity,
        study_id: str,
        ingest_ref: str,
    ) -> tuple[list[NormalizedFinding], NormalizationStats]:
        """Normalize a payload's findings and aggregate telemetry stats."""
        results: list[NormalizedFinding] = []
        stats = NormalizationStats()
        for af in adapter_findings:
            nf, s = self.normalize(af, vendor, study_id, ingest_ref)
            results.append(nf)
            stats = stats.merge(s)
        return results, stats

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _classify(vendor: VendorIdentity) -> _Classification:
        """Proof-based clearance: CLEARED_DEVICE only with a clearance ref."""
        if vendor.has_clearance:
            return _Classification(
                category="EXTERNAL_DETECTION",
                regulatory_class="CLEARED_DEVICE",
                clinical_use_allowed=True,
                disposition_state="PENDING",
                no_clearance_reference=False,
                ruo_label=None,
            )
        # No registered clearance → RUO, research-only, auto-rejected.
        return _Classification(
            category="RESEARCH_ONLY",
            regulatory_class="RUO",
            clinical_use_allowed=False,
            disposition_state="REJECTED",
            no_clearance_reference=True,
            ruo_label=NO_CLEARANCE_RUO_LABEL,
        )

    def _provenance(
        self,
        vendor: VendorIdentity,
        classification: _Classification,
        ingest_ref: str,
    ) -> FindingProvenance:
        return FindingProvenance(
            source="EXTERNAL_CLEARED_AI",
            producer=vendor.producer,
            model_version=vendor.model_version,
            vendor_name=vendor.vendor_name,
            fda_k_number=vendor.fda_k_number,
            ce_mark_ref=vendor.ce_mark_ref,
            runtime=cast("SegmentationRuntime", vendor.runtime),
            produced_at=vendor.produced_at,
            ruo_label=classification.ruo_label,
            ingest_ref=ingest_ref,
        )

    def _measurement(self, m: AdapterMeasurement) -> Measurement:
        return Measurement(name=m.name, value=m.value, unit=m.unit, method=m.method)

    def _geometry(self, geometry: AdapterGeometry | None) -> FindingGeometry | None:
        if geometry is None:
            return None
        return FindingGeometry(
            bbox=list(geometry.bbox) if geometry.bbox else None,
            mask_object_key=geometry.mask_ref,
            point=list(geometry.point) if geometry.point else None,
        )
