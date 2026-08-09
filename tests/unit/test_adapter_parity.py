# ruff: noqa: B008
"""Adapter parity test — DICOM SR, FHIR R4, and Aidoc produce the same Finding.

Three independently-authored adapters — :class:`DicomSRAdapter` (pydicom TID 1500
walk), :class:`FhirR4Adapter` (Observation + ImagingSelection), and
:class:`AidocV1Adapter` (Aidoc JSON) — each parse a *different* payload that
describes the *same* clinical finding.  This test proves they converge on an
identical :class:`AdapterFinding` (criterion 1 — single neutral intermediate)
and, after normalization with the same cleared :class:`VendorIdentity`, an
identical stored :class:`Finding` modulo ``finding_id`` and ``provenance``.

The payloads are deliberately format-distinct (a DICOM binary object, a FHIR
Bundle, and a vendor JSON blob) but encode the same content:

- ``studyInstanceUid`` ``1.2.840…971``
- ``label`` "Pulmonary nodule"
- ``bodySite`` "LUNG"
- one measurement: long-axis diameter 8.0 mm
- bbox ``[10, 20, 110, 120]``
- ``freeText`` "Incidental pulmonary nodule"
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from pydicom import Dataset
from pydicom.dataset import FileMetaDataset
from pydicom.sequence import Sequence as DicomSequence

from app.models.finding import Finding
from app.services.findings_ingest.base import VendorIdentity
from app.services.findings_ingest.dicom_sr import DicomSRAdapter
from app.services.findings_ingest.fhir_r4 import FhirR4Adapter
from app.services.findings_ingest.normalizer import FindingNormalizer, PhiRedactionFilter
from app.services.findings_ingest.vendor.aidoc_v1 import AidocV1Adapter

STUDY_UID = "1.2.840.113619.2.55.3.604688119.971"
SERIES_UID = "1.2.840.113619.2.55.3.604688119.972"
SOP_UID = "1.2.3.4"
LABEL = "Pulmonary nodule"
BODY_SITE = "LUNG"
MEAS_NAME = "long-axis diameter"
MEAS_VALUE = 8.0
MEAS_UNIT = "mm"
BBOX = [10.0, 20.0, 110.0, 120.0]
FREE_TEXT = "Incidental pulmonary nodule"
PRODUCED_AT = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Payload builders — one per adapter, each encoding the same finding
# ---------------------------------------------------------------------------
def _make_dicom_sr_payload() -> bytes:
    """Build a minimal TID 1500 DICOM SR encoding the canonical finding."""

    def _code_meaning(item: Dataset, meaning: str) -> None:
        cn = Dataset()
        cn.CodeMeaning = meaning
        item.ConceptNameCodeSequence = DicomSequence([cn])

    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.22"
    ds.SOPInstanceUID = SOP_UID
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = SERIES_UID
    ds.Modality = "SR"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"

    container = Dataset()
    container.ValueType = "CONTAINER"
    _code_meaning(container, LABEL)

    # Finding Site CODE child → body_site
    site = Dataset()
    site.ValueType = "CODE"
    _code_meaning(site, "Finding Site")
    site_code = Dataset()
    site_code.CodeValue = BODY_SITE
    site_code.CodeMeaning = BODY_SITE
    site.ConceptCodeSequence = DicomSequence([site_code])

    # NUM child → measurement
    num = Dataset()
    num.ValueType = "NUM"
    _code_meaning(num, MEAS_NAME)
    measured = Dataset()
    measured.NumericValue = MEAS_VALUE
    unit = Dataset()
    unit.CodeValue = MEAS_UNIT
    measured.MeasurementUnitsCodeSequence = DicomSequence([unit])
    num.MeasuredValueSequence = DicomSequence([measured])

    # SCOORD child (POLYLINE) → bbox
    scoord = Dataset()
    scoord.ValueType = "SCOORD"
    scoord.GraphicType = "POLYLINE"
    x0, y0, x1, y1 = BBOX
    scoord.GraphicData = [x0, y0, x1, y0, x1, y1, x0, y1]

    # TEXT child → free_text
    text_item = Dataset()
    text_item.ValueType = "TEXT"
    text_item.TextValue = FREE_TEXT

    container.ContentSequence = DicomSequence([site, num, scoord, text_item])
    ds.ContentSequence = DicomSequence([container])

    bio = BytesIO()
    ds.save_as(bio)
    return bio.getvalue()


def _make_fhir_payload() -> bytes:
    """Build a FHIR R4 Bundle encoding the canonical finding."""
    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "DiagnosticReport",
                    "id": "dr-1",
                    "status": "final",
                    "result": [{"reference": "Observation/obs-1"}],
                }
            },
            {
                "resource": {
                    "resourceType": "Observation",
                    "id": "obs-1",
                    "status": "final",
                    "code": {"text": LABEL},
                    "bodySite": {"text": BODY_SITE},
                    "component": [
                        {
                            "code": {"text": MEAS_NAME},
                            "valueQuantity": {"value": MEAS_VALUE, "unit": MEAS_UNIT},
                        }
                    ],
                    "note": [{"text": FREE_TEXT}],
                    "derivedFrom": [{"reference": "ImagingSelection/isel-1"}],
                }
            },
            {
                "resource": {
                    "resourceType": "ImagingSelection",
                    "id": "isel-1",
                    "studyUid": STUDY_UID,
                    "seriesUid": SERIES_UID,
                    "instance": [{"uid": SOP_UID}],
                    "imageRegion": {
                        "coordinate": [
                            [BBOX[0], BBOX[1]],
                            [BBOX[2], BBOX[1]],
                            [BBOX[2], BBOX[3]],
                            [BBOX[0], BBOX[3]],
                        ]
                    },
                }
            },
        ],
    }
    return json.dumps(bundle).encode()


def _make_aidoc_payload() -> bytes:
    """Build an Aidoc JSON payload encoding the canonical finding."""
    aidoc: dict[str, Any] = {
        "studyInstanceUid": STUDY_UID,
        "seriesInstanceUid": SERIES_UID,
        "sopInstanceUids": [SOP_UID],
        "results": [
            {
                "type": LABEL,
                "bodyPart": BODY_SITE,
                "boundingBox": {
                    "x": BBOX[0],
                    "y": BBOX[1],
                    "width": BBOX[2] - BBOX[0],
                    "height": BBOX[3] - BBOX[1],
                },
                "measurements": [{"name": MEAS_NAME, "value": MEAS_VALUE, "unit": MEAS_UNIT}],
                "description": FREE_TEXT,
            }
        ],
    }
    return json.dumps(aidoc).encode()


# (label, adapter, payload-bytes) for each format
PARITY_CASES = [
    ("dicom_sr", DicomSRAdapter(), _make_dicom_sr_payload()),
    ("fhir_r4", FhirR4Adapter(), _make_fhir_payload()),
    ("aidoc_v1", AidocV1Adapter(), _make_aidoc_payload()),
]


def _cleared_vendor() -> VendorIdentity:
    return VendorIdentity(
        vendor_name="Aidoc",
        producer="Aidoc",
        model_version="aidoc-x-1.0",
        adapter_name="aidoc_v1",
        adapter_version="1",
        fda_k_number="K223258",
        produced_at=PRODUCED_AT,
    )


def _normalizer() -> FindingNormalizer:
    return FindingNormalizer(PhiRedactionFilter())


# ---------------------------------------------------------------------------
# Adapter-level parity — identical AdapterFinding
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,adapter,payload", PARITY_CASES, ids=[c[0] for c in PARITY_CASES])
class TestAdapterFindingParity:
    def test_each_adapter_produces_one_finding(
        self, label: str, adapter: Any, payload: bytes
    ) -> None:
        findings = adapter.parse(payload)
        assert len(findings) == 1, f"{label}: expected 1 finding, got {len(findings)}"

    def test_identical_clinical_content(self, label: str, adapter: Any, payload: bytes) -> None:
        f = adapter.parse(payload)[0]
        assert f.study_instance_uid == STUDY_UID
        assert f.label == LABEL
        assert f.body_site == BODY_SITE
        assert f.free_text == FREE_TEXT
        assert f.series_instance_uid == SERIES_UID
        assert f.sop_instance_uids == [SOP_UID]
        # measurements
        assert len(f.measurements) == 1
        m = f.measurements[0]
        assert m.name == MEAS_NAME
        assert m.value == MEAS_VALUE
        assert m.unit == MEAS_UNIT
        assert m.method == ""
        # geometry → bbox
        assert f.geometry is not None
        assert f.geometry.bbox == BBOX

    def test_no_cadt_fields_in_canonical_payloads(
        self, label: str, adapter: Any, payload: bytes
    ) -> None:
        """The canonical payloads carry no CADt fields (clean clinical content)."""
        f = adapter.parse(payload)[0]
        assert f.cadt_fields_present() == []


def test_all_adapters_produce_identical_adapter_finding() -> None:
    """The three adapter findings are value-equal across formats."""
    findings = [adapter.parse(payload)[0] for _label, adapter, payload in PARITY_CASES]
    base = findings[0]
    for i, f in enumerate(findings[1:], 1):
        assert f == base, f"Adapter {PARITY_CASES[i][0]} differs from {PARITY_CASES[0][0]}"


# ---------------------------------------------------------------------------
# Normalizer-level parity — identical Finding modulo finding_id / provenance
# ---------------------------------------------------------------------------
def test_normalized_findings_equal_modulo_id_and_provenance() -> None:
    """After normalization with one cleared vendor, the stored findings match."""
    vendor = _cleared_vendor()
    normalizer = _normalizer()
    findings: list[Finding] = []
    for _label, adapter, payload in PARITY_CASES:
        adapter_findings = adapter.parse(payload)
        normalized, _stats = normalizer.normalize_many(
            adapter_findings, vendor, "st_1", "findings_ingest/fi_test"
        )
        assert len(normalized) == 1
        findings.append(normalized[0].finding)

    # Compare clinical fields (exclude finding_id — a generated ULID — and
    # provenance, which carries adapter identity that we deliberately vary).
    base = findings[0]
    for i, f in enumerate(findings[1:], 1):
        assert f.study_id == base.study_id
        assert f.series_uid == base.series_uid
        assert f.sop_instance_uids == base.sop_instance_uids
        assert f.category == base.category
        assert f.label == base.label
        assert f.body_site == base.body_site
        assert f.measurements == base.measurements
        assert f.geometry == base.geometry
        assert f.regulatory_class == base.regulatory_class
        assert f.clinical_use_allowed == base.clinical_use_allowed
        assert f.disposition == base.disposition
        assert f.finding_id != base.finding_id, "finding_id should be unique per finding"
        assert f.provenance == base.provenance, (
            f"provenance mismatch for {PARITY_CASES[i][0]} vs {PARITY_CASES[0][0]}"
        )


def test_normalized_findings_are_cleared_and_pending() -> None:
    """Every parity finding is CLEARED_DEVICE / clinical-use-allowed / PENDING."""
    vendor = _cleared_vendor()
    normalizer = _normalizer()
    for _label, adapter, payload in PARITY_CASES:
        normalized, stats = normalizer.normalize_many(
            adapter.parse(payload), vendor, "st_1", "findings_ingest/fi_test"
        )
        f = normalized[0].finding
        assert f.regulatory_class == "CLEARED_DEVICE"
        assert f.clinical_use_allowed is True
        assert f.disposition.state == "PENDING"
        assert stats.cadt_fields_dropped == 0
        assert stats.no_clearance_reference_count == 0
