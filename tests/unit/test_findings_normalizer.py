# ruff: noqa: B008
"""Unit tests for the findings normalizer (§3.15.3, §3.15.4 — criteria 2, 3, 4).

Covers:
- **Proof-based clearance**: a registered clearance (FDA K-number OR CE mark) →
  ``CLEARED_DEVICE`` / ``clinicalUseAllowed`` / ``PENDING``; no clearance →
  ``RUO`` / ``RESEARCH_ONLY`` / auto-``REJECTED`` with ``NO_CLEARANCE_REFERENCE``.
- **Never upgrade on the vendor's behalf**: clearance comes only from the
  :class:`VendorIdentity` (registry), never the payload.
- **CADt strip**: suspicion/urgency/triage/priority are dropped and counted; an
  Aidoc payload carrying a triage flag is stripped at the normalizer.
- **PHI redaction**: vendor free text passes :class:`PhiRedactionFilter` before
  it is stored (returned as ``redacted_free_text``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.models.finding import Finding
from app.services.findings_ingest.base import (
    CADT_FIELDS,
    AdapterFinding,
    AdapterGeometry,
    AdapterMeasurement,
    VendorIdentity,
)
from app.services.findings_ingest.normalizer import (
    FindingNormalizer,
    NormalizationStats,
    PhiRedactionFilter,
)
from app.services.findings_ingest.vendor.aidoc_v1 import AidocV1Adapter

PRODUCED_AT = datetime(2026, 1, 1, tzinfo=UTC)
STUDY_UID = "1.2.840.113619.2.55.3.604688119.971"


def _vendor(*, cleared: bool, vendor_name: str = "Aidoc") -> VendorIdentity:
    return VendorIdentity(
        vendor_name=vendor_name,
        producer=vendor_name,
        model_version="x-1.0",
        adapter_name="aidoc_v1",
        adapter_version="1",
        fda_k_number="K223258" if cleared else None,
        ce_mark_ref=None,
        produced_at=PRODUCED_AT,
    )


def _adapter_finding(**overrides: object) -> AdapterFinding:
    base: dict[str, object] = {
        "study_instance_uid": STUDY_UID,
        "label": "Pulmonary nodule",
        "body_site": "LUNG",
        "measurements": [AdapterMeasurement(name="long-axis diameter", value=8.0, unit="mm")],
        "geometry": AdapterGeometry(bbox=[10.0, 20.0, 110.0, 120.0]),
        "free_text": "Incidental pulmonary nodule",
    }
    base.update(overrides)
    return AdapterFinding(**base)  # type: ignore[arg-type]


def _normalizer(known_phi: dict[str, str] | None = None) -> FindingNormalizer:
    return FindingNormalizer(PhiRedactionFilter(known_phi=known_phi))


# ---------------------------------------------------------------------------
# Criterion 2 — proof-based clearance
# ---------------------------------------------------------------------------
class TestProofBasedClearance:
    def test_cleared_vendor_produces_cleared_device(self) -> None:
        nf, stats = _normalizer().normalize(
            _adapter_finding(), _vendor(cleared=True), "st_1", "findings_ingest/x"
        )
        f = nf.finding
        assert f.regulatory_class == "CLEARED_DEVICE"
        assert f.clinical_use_allowed is True
        assert f.category == "EXTERNAL_DETECTION"
        assert f.disposition.state == "PENDING"
        assert f.provenance.source == "EXTERNAL_CLEARED_AI"
        assert f.provenance.fda_k_number == "K223258"
        assert f.provenance.vendor_name == "Aidoc"
        assert f.provenance.ingest_ref == "findings_ingest/x"
        assert stats.no_clearance_reference_count == 0

    def test_ce_mark_alone_clears(self) -> None:
        vendor = VendorIdentity(
            vendor_name="Qure.ai",
            producer="qER",
            model_version="qer-3.0",
            adapter_name="qure_v1",
            adapter_version="1",
            ce_mark_ref="CE-1234-UK",
            produced_at=PRODUCED_AT,
        )
        nf, _ = _normalizer().normalize(_adapter_finding(), vendor, "st_1", "ref")
        assert nf.finding.regulatory_class == "CLEARED_DEVICE"
        assert nf.finding.provenance.ce_mark_ref == "CE-1234-UK"
        assert nf.finding.provenance.fda_k_number is None

    def test_no_clearance_produces_ruo_rejected(self) -> None:
        nf, stats = _normalizer().normalize(
            _adapter_finding(), _vendor(cleared=False, vendor_name="UnknownVendor"), "st_1", "ref"
        )
        f = nf.finding
        assert f.regulatory_class == "RUO"
        assert f.clinical_use_allowed is False
        assert f.category == "RESEARCH_ONLY"
        assert f.disposition.state == "REJECTED"
        assert f.provenance.ruo_label is not None
        assert "clearance" in f.provenance.ruo_label.lower()
        assert stats.no_clearance_reference_count == 1

    def test_never_upgrade_on_vendor_behalf(self) -> None:
        """The payload cannot upgrade a finding — clearance is registry-only.

        An Aidoc payload (which carries no clearance claim at all) normalized
        with a NO-clearance vendor identity is RUO, regardless of source.
        """
        aidoc = {
            "studyInstanceUid": STUDY_UID,
            "results": [
                {
                    "type": "Pulmonary nodule",
                    "bodyPart": "LUNG",
                    "boundingBox": {"x": 10, "y": 20, "width": 100, "height": 100},
                    "triage": "positive",
                }
            ],
        }
        findings = AidocV1Adapter().parse(json.dumps(aidoc).encode())
        nf, stats = _normalizer().normalize(
            findings[0], _vendor(cleared=False, vendor_name="Unverified"), "st_1", "ref"
        )
        assert nf.finding.regulatory_class == "RUO"
        assert nf.finding.clinical_use_allowed is False
        assert stats.no_clearance_reference_count == 1


# ---------------------------------------------------------------------------
# Criterion 3 — CADt strip
# ---------------------------------------------------------------------------
class TestCadtStrip:
    def test_cadt_fields_present_detected(self) -> None:
        af = _adapter_finding(suspicion=0.9, urgency="high", triage="positive", priority="urgent")
        assert set(af.cadt_fields_present()) == {"suspicion", "urgency", "triage", "priority"}

    def test_cadt_fields_dropped_and_counted(self) -> None:
        af = _adapter_finding(suspicion=0.9, urgency="high", triage="positive", priority="urgent")
        nf, stats = _normalizer().normalize(af, _vendor(cleared=True), "st_1", "ref")
        assert stats.cadt_fields_dropped == 4
        dump = nf.finding.model_dump()
        for field in CADT_FIELDS:
            assert field not in dump, f"Finding leaked CADt field {field}"
            assert field not in dump["provenance"], f"Provenance leaked CADt field {field}"

    def test_no_cadt_fields_zero_count(self) -> None:
        af = _adapter_finding()
        _nf, stats = _normalizer().normalize(af, _vendor(cleared=True), "st_1", "ref")
        assert stats.cadt_fields_dropped == 0
        assert stats.no_clearance_reference_count == 0

    def test_aidoc_triage_payload_stripped(self) -> None:
        """An Aidoc payload with a triage flag is stripped at the normalizer."""
        aidoc = {
            "studyInstanceUid": STUDY_UID,
            "results": [
                {
                    "type": "Intracranial hemorrhage",
                    "bodyPart": "BRAIN",
                    "boundingBox": {"x": 5, "y": 5, "width": 40, "height": 40},
                    "urgency": "positive",
                    "triage": "positive",
                    "suspicion": 0.92,
                }
            ],
        }
        findings = AidocV1Adapter().parse(json.dumps(aidoc).encode())
        normalized, stats = _normalizer().normalize_many(
            findings, _vendor(cleared=True), "st_1", "ref"
        )
        assert stats.cadt_fields_dropped == 3  # urgency + triage + suspicion
        f = normalized[0].finding
        # CADt does not affect disposition — a cleared finding stays PENDING.
        assert f.disposition.state == "PENDING"
        assert f.regulatory_class == "CLEARED_DEVICE"
        dump = f.model_dump()
        for field in CADT_FIELDS:
            assert field not in dump

    def test_cadt_does_not_affect_disposition(self) -> None:
        af = _adapter_finding(triage="positive", urgency="stat")
        nf, _ = _normalizer().normalize(af, _vendor(cleared=True), "st_1", "ref")
        assert nf.finding.disposition.state == "PENDING"


# ---------------------------------------------------------------------------
# Criterion 4 — PHI redaction of free text
# ---------------------------------------------------------------------------
class TestPhiRedaction:
    def test_known_phi_redacted_from_free_text(self) -> None:
        normalizer = _normalizer(
            known_phi={"patient_name": "Doe, John", "mrn": "MRN-4471"}
        )
        af = _adapter_finding(free_text="Nodule noted for Doe, John (MRN-4471)")
        nf, _ = normalizer.normalize(af, _vendor(cleared=True), "st_1", "ref")
        assert nf.redacted_free_text is not None
        assert "Doe, John" not in nf.redacted_free_text
        assert "MRN-4471" not in nf.redacted_free_text
        assert "[REDACTED]" in nf.redacted_free_text

    def test_clean_free_text_unchanged(self) -> None:
        normalizer = _normalizer()
        af = _adapter_finding(free_text="Incidental pulmonary nodule")
        nf, _ = normalizer.normalize(af, _vendor(cleared=True), "st_1", "ref")
        assert nf.redacted_free_text == "Incidental pulmonary nodule"

    def test_no_free_text_yields_none(self) -> None:
        af = _adapter_finding(free_text=None)
        nf, _ = _normalizer().normalize(af, _vendor(cleared=True), "st_1", "ref")
        assert nf.redacted_free_text is None

    def test_redaction_filter_patterns(self) -> None:
        f = PhiRedactionFilter(known_phi={"patient_name": "Jane Doe"})
        assert "Jane Doe" not in f.filter("Seen Jane Doe in clinic")
        assert "[REDACTED_DATE]" in f.filter("born 1985-03-02")
        assert "[REDACTED]" in f.filter("email a@b.com please")
        # Clean clinical prose is untouched.
        assert f.filter("8 mm solid nodule") == "8 mm solid nodule"


# ---------------------------------------------------------------------------
# Provenance stamping
# ---------------------------------------------------------------------------
class TestProvenance:
    def test_provenance_carries_vendor_and_ingest_ref(self) -> None:
        vendor = _vendor(cleared=True)
        vendor = vendor.model_copy(
            update={"payload_ref": "findings_ingest/fi_abc/source.dcm"}
        )
        nf, _ = _normalizer().normalize(
            _adapter_finding(), vendor, "st_1", "findings_ingest/fi_abc"
        )
        p = nf.finding.provenance
        assert p.source == "EXTERNAL_CLEARED_AI"
        assert p.vendor_name == "Aidoc"
        assert p.runtime == "external"
        assert p.ingest_ref == "findings_ingest/fi_abc"
        assert p.produced_at == PRODUCED_AT

    def test_finding_is_stored_shape(self) -> None:
        nf, _ = _normalizer().normalize(
            _adapter_finding(), _vendor(cleared=True), "st_1", "ref", finding_id="fd_TEST"
        )
        assert isinstance(nf.finding, Finding)
        assert nf.finding.finding_id == "fd_TEST"
        assert nf.finding.study_id == "st_1"

    def test_stats_merge(self) -> None:
        a = NormalizationStats(cadt_fields_dropped=2, no_clearance_reference_count=1)
        b = NormalizationStats(cadt_fields_dropped=3, no_clearance_reference_count=0)
        merged = a.merge(b)
        assert merged.cadt_fields_dropped == 5
        assert merged.no_clearance_reference_count == 1
