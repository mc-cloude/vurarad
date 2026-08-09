"""Unit tests for the Finding model (§3.15.1 — acceptance criteria 1, 2, 8).

Covers:
- RUO separation: ``regulatoryClass == "RUO"`` forces ``clinicalUseAllowed ==
  False`` and requires a non-null ``ruoLabel``.
- ``EXTERNAL_DETECTION`` requires ``EXTERNAL_CLEARED_AI`` source + FDA K-number
  or CE mark ref.
- ``DispositionRequest`` requires ``confirmedText`` for CONFIRMED/EDITED.
- ``FindingsResponse`` shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.finding import (
    Disposition,
    DispositionRequest,
    Finding,
    FindingProvenance,
    FindingsResponse,
    Measurement,
    UnavailableReason,
)


def _provenance(**overrides: object) -> FindingProvenance:
    base: dict[str, object] = {
        "source": "VURARAD_SEGMENTATION",
        "producer": "TotalSegmentator",
        "model_version": "totalsegmentator-2.4.0",
        "runtime": "cpu_fast",
        "produced_at": datetime.now(UTC),
    }
    base.update(overrides)
    return FindingProvenance(**base)  # type: ignore[arg-type]


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "finding_id": "fd_01TEST",
        "study_id": "st_test",
        "category": "ANATOMICAL_MEASUREMENT",
        "label": "Liver",
        "provenance": _provenance(),
        "regulatory_class": "MEASUREMENT",
        "clinical_use_allowed": True,
    }
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Criterion 1 — RUO separation
# ---------------------------------------------------------------------------
class TestRuoSeparation:
    def test_ruo_forces_clinical_use_false(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _finding(
                regulatory_class="RUO",
                clinical_use_allowed=True,
                provenance=_provenance(ruo_label="Research use only"),
            )
        assert "ruo_clinical_use" in str(exc_info.value)

    def test_ruo_requires_ruo_label(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _finding(
                regulatory_class="RUO",
                clinical_use_allowed=False,
                provenance=_provenance(ruo_label=None),
            )
        assert "ruo_label_required" in str(exc_info.value)

    def test_ruo_with_label_and_false_clinical_use_ok(self) -> None:
        f = _finding(
            regulatory_class="RUO",
            clinical_use_allowed=False,
            provenance=_provenance(
                ruo_label="For research use only — not for clinical decision-making"
            ),
        )
        assert f.regulatory_class == "RUO"
        assert f.clinical_use_allowed is False
        assert f.provenance.ruo_label is not None

    def test_measurement_allows_clinical_use(self) -> None:
        f = _finding(regulatory_class="MEASUREMENT", clinical_use_allowed=True)
        assert f.clinical_use_allowed is True

    def test_cleared_device_allows_clinical_use(self) -> None:
        f = _finding(
            regulatory_class="CLEARED_DEVICE",
            clinical_use_allowed=True,
            provenance=_provenance(
                source="EXTERNAL_CLEARED_AI",
                vendor_name="Aidoc",
                fda_k_number="K223258",
            ),
        )
        assert f.clinical_use_allowed is True


# ---------------------------------------------------------------------------
# Criterion 2 — EXTERNAL_DETECTION requires external source + clearance
# ---------------------------------------------------------------------------
class TestExternalDetection:
    def test_requires_external_source(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _finding(
                category="EXTERNAL_DETECTION",
                provenance=_provenance(
                    source="VURARAD_SEGMENTATION",
                    fda_k_number="K223258",
                ),
            )
        assert "external_detection_source" in str(exc_info.value)

    def test_requires_clearance_ref(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _finding(
                category="EXTERNAL_DETECTION",
                provenance=_provenance(
                    source="EXTERNAL_CLEARED_AI",
                    vendor_name="Aidoc",
                ),
            )
        assert "external_detection_clearance" in str(exc_info.value)

    def test_with_fda_k_number_ok(self) -> None:
        f = _finding(
            category="EXTERNAL_DETECTION",
            label="Right upper lobe nodule",
            provenance=_provenance(
                source="EXTERNAL_CLEARED_AI",
                vendor_name="Aidoc",
                fda_k_number="K223258",
            ),
        )
        assert f.category == "EXTERNAL_DETECTION"

    def test_with_ce_mark_ref_ok(self) -> None:
        f = _finding(
            category="EXTERNAL_DETECTION",
            label="Intracranial hemorrhage",
            provenance=_provenance(
                source="EXTERNAL_CLEARED_AI",
                vendor_name="Qure.ai",
                ce_mark_ref="CE-12345",
            ),
        )
        assert f.category == "EXTERNAL_DETECTION"


# ---------------------------------------------------------------------------
# Criterion 8 — DispositionRequest confirmedText requirement
# ---------------------------------------------------------------------------
class TestDispositionRequest:
    def test_confirmed_requires_text(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DispositionRequest(state="CONFIRMED")
        assert "confirmed_text_required" in str(exc_info.value)

    def test_edited_requires_text(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DispositionRequest(state="EDITED")
        assert "confirmed_text_required" in str(exc_info.value)

    def test_confirmed_with_text_ok(self) -> None:
        req = DispositionRequest(state="CONFIRMED", confirmed_text="8 mm solid nodule")
        assert req.state == "CONFIRMED"
        assert req.confirmed_text == "8 mm solid nodule"

    def test_rejected_without_text_ok(self) -> None:
        req = DispositionRequest(state="REJECTED")
        assert req.state == "REJECTED"
        assert req.confirmed_text is None

    def test_edited_with_text_ok(self) -> None:
        req = DispositionRequest(state="EDITED", confirmed_text="6 mm part-solid nodule")
        assert req.state == "EDITED"


# ---------------------------------------------------------------------------
# FindingsResponse shape
# ---------------------------------------------------------------------------
class TestFindingsResponse:
    def test_empty_response(self) -> None:
        resp = FindingsResponse(
            study_id="st_test",
            generated_at=datetime.now(UTC),
        )
        assert resp.findings == []
        assert resp.unavailable_reasons == []
        assert resp.preprocessing_state == "COMPLETE"

    def test_populated_response(self) -> None:
        f = _finding()
        resp = FindingsResponse(
            study_id="st_test",
            generated_at=datetime.now(UTC),
            findings=[f],
            unavailable_reasons=[
                UnavailableReason(
                    capability="segmentation",
                    reason="NO_MODEL_AVAILABLE",
                    detail="No model for US",
                )
            ],
        )
        assert len(resp.findings) == 1
        assert resp.unavailable_reasons[0].reason == "NO_MODEL_AVAILABLE"


# ---------------------------------------------------------------------------
# Measurement + Disposition defaults
# ---------------------------------------------------------------------------
class TestSubModels:
    def test_measurement(self) -> None:
        m = Measurement(value=1450.0, unit="mL", method="3D volume")
        assert m.value == 1450.0
        assert m.unit == "mL"

    def test_disposition_defaults_to_pending(self) -> None:
        d = Disposition()
        assert d.state == "PENDING"
        assert d.by_uid is None
        assert d.confirmed_text is None
