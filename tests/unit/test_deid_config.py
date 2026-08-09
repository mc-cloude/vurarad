"""Unit tests for the de-id settings — production guard + parsed properties.

Covers acceptance criterion 6: ``deid_require_pixel_pass`` cannot be ``False``
in production.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings

VALID_KWARGS: dict[str, object] = {
    "gcp_project_id": "vurarad-test",
    "gcp_region": "us-central1",
    "pixel_bucket_name": "pixels",
    "audit_bucket_name": "audit",
    "firebase_project_id": "vurarad-test",
}


def _kwargs(**overrides: object) -> dict[str, object]:
    return {**VALID_KWARGS, **overrides}


# ---------------------------------------------------------------------------
# Criterion 6 — deid_require_pixel_pass cannot be False in production
# ---------------------------------------------------------------------------
def test_pixel_pass_false_rejected_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **_kwargs(
                environment=Environment.production,
                deid_require_pixel_pass=False,
            )
        )


def test_pixel_pass_true_allowed_in_production() -> None:
    s = Settings(**_kwargs(environment=Environment.production, deid_require_pixel_pass=True))
    assert s.deid_require_pixel_pass is True


def test_pixel_pass_false_allowed_in_non_production() -> None:
    s = Settings(**_kwargs(deid_require_pixel_pass=False))
    assert s.deid_require_pixel_pass is False


# ---------------------------------------------------------------------------
# Recall floors — present and correct
# ---------------------------------------------------------------------------
def test_recall_floors() -> None:
    s = Settings(**_kwargs())
    assert s.deid_recall_floor_us_sc_ot_xc == 0.99
    assert s.deid_recall_floor_cr_dx_mg == 0.98
    assert s.deid_recall_floor_ct_mr == 0.97


# ---------------------------------------------------------------------------
# Engine / classifier defaults are dependency-free
# ---------------------------------------------------------------------------
def test_engine_defaults_are_dependency_free() -> None:
    s = Settings(**_kwargs())
    assert s.deid_ocr_engine == "threshold"
    assert s.deid_phi_classifier == "deterministic"


def test_confidence_threshold_default() -> None:
    assert Settings(**_kwargs()).deid_confidence_threshold == 0.9


# ---------------------------------------------------------------------------
# Parsed properties
# ---------------------------------------------------------------------------
class TestForcedReviewModalities:
    def test_empty_by_default(self) -> None:
        assert Settings(**_kwargs()).deid_forced_review_modalities == frozenset()

    def test_parses_upper_cased(self) -> None:
        s = Settings(**_kwargs(deid_modalities_forced_review="us, sc ,OT"))
        assert s.deid_forced_review_modalities == frozenset({"US", "SC", "OT"})

    def test_ignores_empty_entries(self) -> None:
        s = Settings(**_kwargs(deid_modalities_forced_review="US,, ,SC"))
        assert s.deid_forced_review_modalities == frozenset({"US", "SC"})


class TestValidatedSourcePairs:
    def test_empty_by_default(self) -> None:
        assert Settings(**_kwargs()).deid_validated_source_pairs == frozenset()

    def test_parses_modality_manufacturer_pairs(self) -> None:
        s = Settings(**_kwargs(deid_validated_sources="US:Acme, CR:GE Healthcare"))
        assert s.deid_validated_source_pairs == frozenset({("US", "Acme"), ("CR", "GE Healthcare")})

    def test_modality_upper_cased_manufacturer_preserved(self) -> None:
        s = Settings(**_kwargs(deid_validated_sources="us:Philips"))
        assert ("US", "Philips") in s.deid_validated_source_pairs

    def test_ignores_entries_without_colon(self) -> None:
        s = Settings(**_kwargs(deid_validated_sources="US:Acme, bogus, CR:GE"))
        assert s.deid_validated_source_pairs == frozenset({("US", "Acme"), ("CR", "GE")})
