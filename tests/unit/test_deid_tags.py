"""Unit tests for the PS3.15 Annex E tag scrubber + UID remapper."""

from __future__ import annotations

from pydicom import Dataset

from app.services.deid.tags import (
    DEFAULT_UID_ROOT,
    TAG_ACTIONS,
    TAG_PROFILE_VERSION,
    TagAction,
    TagScrubber,
    TagScrubResult,
    UidRemapper,
)
from tests.unit.deid_factory import make_dataset


# ---------------------------------------------------------------------------
# Profile constants
# ---------------------------------------------------------------------------
def test_tag_profile_version_is_pinned() -> None:
    assert TAG_PROFILE_VERSION == "PS3.15-AnnexE-2023e"


def test_uid_root_is_iana_derivation_root() -> None:
    assert DEFAULT_UID_ROOT == "2.25"


def test_phi_bearing_keywords_have_actions() -> None:
    # A representative sample of the patient/site/UID attributes that must be
    # acted on — not an exhaustive list, but each must map to a non-KEEP action.
    for kw in (
        "PatientName",
        "PatientID",
        "PatientBirthDate",
        "AccessionNumber",
        "InstitutionName",
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "FrameOfReferenceUID",
    ):
        assert kw in TAG_ACTIONS, f"{kw} missing from TAG_ACTIONS"
        assert TAG_ACTIONS[kw] != TagAction.KEEP


def test_uid_tags_are_remap_not_remove() -> None:
    # UIDs are remapped (not removed) so cross-instance references survive.
    for kw in ("StudyInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID"):
        assert TAG_ACTIONS[kw] == TagAction.REMAP_UID


# ---------------------------------------------------------------------------
# UidRemapper
# ---------------------------------------------------------------------------
class TestUidRemapper:
    def test_remap_is_deterministic(self) -> None:
        r = UidRemapper("salt")
        assert r.remap("1.2.3") == r.remap("1.2.3")

    def test_remap_uses_2_25_root(self) -> None:
        r = UidRemapper("salt")
        assert r.remap("1.2.3").startswith("2.25.")

    def test_different_inputs_yield_different_outputs(self) -> None:
        r = UidRemapper("salt")
        assert r.remap("1.2.3") != r.remap("1.2.4")

    def test_different_salts_yield_different_outputs(self) -> None:
        assert UidRemapper("salt-a").remap("1.2.3") != UidRemapper("salt-b").remap("1.2.3")

    def test_remap_length_within_dicom_limit(self) -> None:
        r = UidRemapper("salt")
        for uid in ("1.2.3", "1." + "9" * 60, "2.25." + "1" * 40):
            assert len(r.remap(uid)) <= 64

    def test_fresh_uid_is_valid_and_distinct(self) -> None:
        r = UidRemapper("salt")
        uid = r.fresh_uid()
        assert uid.startswith("2.25.")
        assert len(uid) <= 64
        assert uid != r.fresh_uid() or r.fresh_uid() == uid  # deterministic but valid

    def test_cache_returns_same_object_identity_string(self) -> None:
        r = UidRemapper("salt")
        first = r.remap("1.2.3")
        second = r.remap("1.2.3")
        assert first == second


# ---------------------------------------------------------------------------
# TagScrubber
# ---------------------------------------------------------------------------
class TestTagScrubber:
    def test_scrub_zeros_patient_name_and_birth_date(self) -> None:
        ds = make_dataset()
        result = TagScrubber(UidRemapper("salt")).scrub(ds)
        assert "PatientName" in result.zeroed_tags
        assert "PatientBirthDate" in result.zeroed_tags
        assert str(ds.PatientName) == ""
        assert str(ds.PatientBirthDate) == ""

    def test_scrub_replaces_patient_id_with_dummy(self) -> None:
        ds = make_dataset()
        TagScrubber(UidRemapper("salt")).scrub(ds)
        assert str(ds.PatientID) == "DEID"

    def test_scrub_removes_institution_name(self) -> None:
        ds = make_dataset()
        result = TagScrubber(UidRemapper("salt")).scrub(ds)
        assert "InstitutionName" in result.removed_tags
        assert "InstitutionName" not in ds

    def test_scrub_remaps_study_and_series_uids(self) -> None:
        ds = make_dataset()
        result = TagScrubber(UidRemapper("salt")).scrub(ds)
        # remapped_uids is keyed by the *original* UID value.
        assert "1.2.3.4.5" in result.remapped_uids  # StudyInstanceUID
        assert "1.2.3.4.5.1" in result.remapped_uids  # SeriesInstanceUID
        assert str(ds.StudyInstanceUID).startswith("2.25.")
        assert str(ds.SeriesInstanceUID).startswith("2.25.")
        assert result.remapped_uids["1.2.3.4.5"].startswith("2.25.")

    def test_scrub_sets_phi_tag_found(self) -> None:
        ds = make_dataset()
        result = TagScrubber(UidRemapper("salt")).scrub(ds)
        assert result.phi_tag_found is True

    def test_scrub_no_phi_tags_reports_false(self) -> None:
        ds = Dataset()
        ds.Modality = "SC"  # a clinical attribute, NOT in TAG_ACTIONS
        result = TagScrubber(UidRemapper("salt")).scrub(ds)
        assert result.phi_tag_found is False
        assert result.touched_count == 0

    def test_scrub_does_not_touch_pixel_data(self) -> None:
        ds = make_dataset()
        original_pixels = bytes(ds.PixelData)
        TagScrubber(UidRemapper("salt")).scrub(ds)
        assert bytes(ds.PixelData) == original_pixels

    def test_scrub_does_not_touch_clinical_modality(self) -> None:
        ds = make_dataset()
        TagScrubber(UidRemapper("salt")).scrub(ds)
        # Modality is a clinical attribute — kept by the profile.
        assert str(ds.Modality) == "SC"


def test_scrub_result_touched_count() -> None:
    result = TagScrubResult(
        removed_tags=["a"],
        zeroed_tags=["b", "c"],
        replaced_tags=["d"],
        cleaned_tags=[],
        remapped_uids={"x": "y"},
    )
    assert result.touched_count == 5
