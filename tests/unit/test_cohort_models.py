"""Cohort model validation (WP17 — criterion 7, model invariants)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.cohort import (
    AddSubjectRequest,
    Cohort,
    CohortSegmentation,
    CohortStatus,
    CohortSubject,
    CohortSubjectStatus,
    CreateCohortRequest,
    CreateSegmentationRequest,
    DeidReviewItem,
    DeidReviewStatus,
    SegmentationSource,
    SegmentationVersion,
)


def _valid_cohort_kwargs() -> dict[str, object]:
    return {
        "cohort_id": "co_01",
        "name": "NSC-Lung",
        "irb_reference": "IRB-2026-0142",
        "irb_determination": "EXEMPT",
        "created_by": "op_1",
        "created_at": "2026-08-09T00:00:00Z",
        "updated_at": "2026-08-09T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Cohort — IRB fields required (criterion 7)
# ---------------------------------------------------------------------------
class TestCohortModel:
    def test_valid_cohort(self) -> None:
        cohort = Cohort(**_valid_cohort_kwargs())
        assert cohort.cohort_id == "co_01"
        assert cohort.status == CohortStatus.ACTIVE
        assert cohort.regulatory_class == "RUO"
        assert cohort.retention_days == 365

    def test_irb_reference_required(self) -> None:
        kw = _valid_cohort_kwargs()
        kw["irb_reference"] = ""
        with pytest.raises(ValidationError):
            Cohort(**kw)

    def test_irb_determination_required(self) -> None:
        kw = _valid_cohort_kwargs()
        kw["irb_determination"] = ""  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            Cohort(**kw)

    def test_irb_determination_must_be_known(self) -> None:
        kw = _valid_cohort_kwargs()
        kw["irb_determination"] = "BOGUS"  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            Cohort(**kw)

    def test_missing_irb_reference_raises(self) -> None:
        kw = _valid_cohort_kwargs()
        del kw["irb_reference"]
        with pytest.raises(ValidationError):
            Cohort(**kw)

    def test_camelcase_aliases(self) -> None:
        cohort = Cohort(**_valid_cohort_kwargs())
        dumped = cohort.model_dump(by_alias=True)
        assert "cohortId" in dumped
        assert "irbReference" in dumped
        assert "irbDetermination" in dumped
        assert "regulatoryClass" in dumped

    def test_no_study_or_patient_key_fields(self) -> None:
        fields = set(Cohort.model_fields.keys())
        assert "study_id" not in fields
        assert "patient_key" not in fields


# ---------------------------------------------------------------------------
# CohortSubject
# ---------------------------------------------------------------------------
class TestCohortSubjectModel:
    def _kwargs(self) -> dict[str, object]:
        return {
            "subject_id": "cs_01",
            "cohort_id": "co_01",
            "deid_object_path": "deid/cs_01/volume.nii",
            "pixel_pass_passed": True,
            "created_at": "2026-08-09T00:00:00Z",
            "updated_at": "2026-08-09T00:00:00Z",
        }

    def test_defaults(self) -> None:
        subject = CohortSubject(**self._kwargs())
        assert subject.status == CohortSubjectStatus.PENDING_REVIEW
        assert subject.review_items == []
        assert subject.source_kind == "WORKLIST"

    def test_no_study_or_patient_key_fields(self) -> None:
        fields = set(CohortSubject.model_fields.keys())
        assert "study_id" not in fields
        assert "patient_key" not in fields

    def test_carries_review_items(self) -> None:
        item = DeidReviewItem(item_id="ri_1", status=DeidReviewStatus.OPEN)
        subject = CohortSubject(**{**self._kwargs(), "review_items": [item]})
        assert subject.review_items[0].status == DeidReviewStatus.OPEN

    def test_camelcase_aliases(self) -> None:
        subject = CohortSubject(**self._kwargs())
        dumped = subject.model_dump(by_alias=True)
        assert "subjectId" in dumped
        assert "deidObjectPath" in dumped
        assert "pixelPassPassed" in dumped


# ---------------------------------------------------------------------------
# Segmentation — versioned, editor recorded (criterion 6)
# ---------------------------------------------------------------------------
class TestSegmentationModel:
    def test_version_records_editor(self) -> None:
        v = SegmentationVersion(
            version=1,
            mask_object_path="deid/co_01/cs_01/seg/v1.nii",
            editor="op_1",
            source=SegmentationSource.MONAI,
            created_at="2026-08-09T00:00:00Z",
        )
        assert v.editor == "op_1"
        assert v.source == SegmentationSource.MONAI

    def test_segmentation_defaults(self) -> None:
        seg = CohortSegmentation(
            segmentation_id="sg_01",
            cohort_id="co_01",
            subject_id="cs_01",
            created_at="2026-08-09T00:00:00Z",
            updated_at="2026-08-09T00:00:00Z",
        )
        assert seg.current_version == 0
        assert seg.versions == []
        assert seg.regulatory_class == "RUO"

    def test_segmentation_no_study_or_patient_key_fields(self) -> None:
        fields = set(CohortSegmentation.model_fields.keys())
        assert "study_id" not in fields
        assert "patient_key" not in fields

    def test_versions_are_append_only_by_construction(self) -> None:
        v1 = SegmentationVersion(
            version=1,
            mask_object_path="deid/co_01/cs_01/seg/v1.nii",
            editor="op_1",
            source=SegmentationSource.MONAI,
            created_at="2026-08-09T00:00:00Z",
        )
        v2 = SegmentationVersion(
            version=2,
            mask_object_path="deid/co_01/cs_01/seg/v2.nii",
            editor="op_2",
            source=SegmentationSource.MANUAL_EDIT,
            created_at="2026-08-09T00:01:00Z",
        )
        seg = CohortSegmentation(
            segmentation_id="sg_01",
            cohort_id="co_01",
            subject_id="cs_01",
            current_version=2,
            versions=[v1, v2],
            created_at="2026-08-09T00:00:00Z",
            updated_at="2026-08-09T00:01:00Z",
        )
        assert seg.current_version == 2
        assert seg.versions[0].mask_object_path.endswith("v1.nii")
        assert seg.versions[1].mask_object_path.endswith("v2.nii")
        # Versioned paths are distinct — never overwritten.
        assert seg.versions[0].mask_object_path != seg.versions[1].mask_object_path


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class TestRequestModels:
    def test_create_cohort_request_requires_irb(self) -> None:
        with pytest.raises(ValidationError):
            CreateCohortRequest(name="x", irb_reference="", irb_determination="EXEMPT")

    def test_create_cohort_request_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            CreateCohortRequest(name="", irb_reference="IRB-1", irb_determination="EXEMPT")

    def test_add_subject_worklist_requires_study_id(self) -> None:
        with pytest.raises(ValidationError):
            AddSubjectRequest(source_kind="WORKLIST", study_id="")

    def test_add_subject_upload_requires_upload_ref(self) -> None:
        with pytest.raises(ValidationError):
            AddSubjectRequest(source_kind="UPLOAD", upload_ref="")

    def test_add_subject_worklist_with_study_id_ok(self) -> None:
        req = AddSubjectRequest(source_kind="WORKLIST", study_id="st_test")
        assert req.source_kind == "WORKLIST"

    def test_create_segmentation_request_defaults(self) -> None:
        req = CreateSegmentationRequest(subject_id="cs_01")
        assert req.source == SegmentationSource.MONAI
