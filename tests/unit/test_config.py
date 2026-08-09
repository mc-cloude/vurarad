"""Settings startup validation — region allowlist, production emulator guard."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import (
    L4_GPU_REGIONS,
    VERTEX_ALLOWLIST,
    Environment,
    ResidencyPolicy,
    Settings,
)

# A complete, valid kwargs bundle for constructing ad-hoc Settings instances.
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
# Required-field enforcement
# ---------------------------------------------------------------------------
def test_refuses_to_start_without_required_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing required field raises ValidationError at construction."""
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(
            gcp_region="us-central1",
            pixel_bucket_name="pixels",
            audit_bucket_name="audit",
            firebase_project_id="vurarad-test",
        )


# ---------------------------------------------------------------------------
# Region allowlist
# ---------------------------------------------------------------------------
def test_rejects_disallowed_region() -> None:
    """me-central1 is NOT in the Vertex AI allowlist → rejected."""
    assert "me-central1" not in VERTEX_ALLOWLIST
    with pytest.raises(ValidationError):
        Settings(**_kwargs(gcp_region="me-central1"))


def test_accepts_allowed_region() -> None:
    s = Settings(**_kwargs(gcp_region="europe-west1"))
    assert s.gcp_region == "europe-west1"


def test_region_check_is_case_insensitive() -> None:
    s = Settings(**_kwargs(gcp_region="US-CENTRAL1"))
    assert s.gcp_region == "US-CENTRAL1"


# ---------------------------------------------------------------------------
# Production emulator guard
# ---------------------------------------------------------------------------
def test_rejects_emulator_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **_kwargs(
                environment=Environment.production,
                firestore_emulator_host="localhost:8200",
            )
        )


def test_allows_emulator_in_development() -> None:
    s = Settings(**_kwargs(firestore_emulator_host="localhost:8200"))
    assert s.firestore_emulator_host == "localhost:8200"


def test_production_without_emulator_is_valid() -> None:
    s = Settings(**_kwargs(environment=Environment.production))
    assert s.is_production is True
    assert s.firestore_emulator_host is None


# ---------------------------------------------------------------------------
# vertex_location override
# ---------------------------------------------------------------------------
def test_vertex_location_overrides_to_region_in_production() -> None:
    s = Settings(**_kwargs(environment=Environment.production, gcp_region="europe-west1"))
    assert s.vertex_location == "europe-west1"


def test_default_vertex_location_kept_in_development() -> None:
    s = Settings(**_kwargs())
    assert s.vertex_location == "us-central1"


def test_custom_vertex_location_preserved() -> None:
    s = Settings(**_kwargs(vertex_location="asia-south1"))
    assert s.vertex_location == "asia-south1"


# ---------------------------------------------------------------------------
# Residency / segmentation region guard
# ---------------------------------------------------------------------------
def test_segmentation_region_defaults_to_gcp_region() -> None:
    s = Settings(**_kwargs(gcp_region="us-east4"))
    assert s.segmentation_region == "us-east4"


def test_rejects_bad_segmentation_region_for_africa_residency() -> None:
    with pytest.raises(ValidationError):
        Settings(**_kwargs(segmentation_region="me-central1"))


def test_non_africa_residency_allows_any_segmentation_region() -> None:
    s = Settings(
        **_kwargs(
            residency_policy=ResidencyPolicy.europe,
            segmentation_region="me-central1",
        )
    )
    assert s.segmentation_region == "me-central1"


# ---------------------------------------------------------------------------
# Derived properties / helpers
# ---------------------------------------------------------------------------
def test_is_cloud_run_gpu_available_true() -> None:
    s = Settings(**_kwargs(gcp_region="us-central1"))
    assert s.is_cloud_run_gpu_available is True
    assert "us-central1" in L4_GPU_REGIONS


def test_is_cloud_run_gpu_available_false() -> None:
    s = Settings(**_kwargs(gcp_region="africa-south1"))
    assert s.is_cloud_run_gpu_available is False
    assert "africa-south1" not in L4_GPU_REGIONS


def test_cors_origins_empty() -> None:
    s = Settings(**_kwargs())
    assert s.cors_origins == []


def test_cors_origins_parsed() -> None:
    s = Settings(**_kwargs(cors_allow_origins="https://a.example, https://b.example"))
    assert s.cors_origins == ["https://a.example", "https://b.example"]


def test_is_production_flag() -> None:
    assert Settings(**_kwargs()).is_production is False
    assert Settings(**_kwargs(environment=Environment.production)).is_production is True


def test_assertion_fingerprint_is_deterministic_and_short() -> None:
    s = Settings(**_kwargs(gcp_region="us-central1"))
    fp = s.assertion_fingerprint()
    assert len(fp) == 16
    assert s.assertion_fingerprint() == fp
    # A different region yields a different fingerprint.
    s2 = Settings(**_kwargs(gcp_region="europe-west1"))
    assert s2.assertion_fingerprint() != fp
