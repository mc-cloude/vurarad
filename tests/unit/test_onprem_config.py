"""On-prem hosting configuration — residency guard and required dependencies.

Acceptance criterion 6: "Residency: an on-prem deployment cannot be configured
to call a cloud region. Config validator test."

The validator in ``app.core.config.Settings._validate_env`` enforces that an
on-prem deployment:

- does NOT target a cloud Vertex AI region for segmentation (residency),
- uses MinIO (not GCS) for object storage,
- stores metadata in PostgreSQL (``postgres_dsn``),
- verifies auth locally via OIDC (``oidc_issuer``, ``oidc_audience``, and a
  key source — ``oidc_jwks_path`` or ``oidc_hmac_secret``).

No outbound cloud dependency is valid for steady-state operation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import VERTEX_ALLOWLIST, Settings

# A complete, valid kwargs bundle for the **cloud** tier (mirrors test_config).
_CLOUD_KWARGS: dict[str, object] = {
    "gcp_project_id": "vurarad-test",
    "gcp_region": "us-central1",
    "pixel_bucket_name": "pixels",
    "audit_bucket_name": "audit",
    "firebase_project_id": "vurarad-test",
}

# A complete, valid kwargs bundle for the **on-prem** tier.
# ``gcp_region`` is an opaque site label — it must NOT be a Vertex allowlist
# region (the allowlist check is skipped for onprem, but the on-prem block
# rejects ``segmentation_region`` inside the allowlist).
_ONPREM_KWARGS: dict[str, object] = {
    "gcp_project_id": "vurarad-onprem",
    "gcp_region": "local",
    "pixel_bucket_name": "pixels",
    "audit_bucket_name": "audit",
    "firebase_project_id": "vurarad-onprem",
    "hosting": "onprem",
    "storage_backend": "minio",
    "minio_endpoint": "http://minio:9000",
    "minio_access_key": "minioadmin",
    "minio_secret_key": "minioadmin",
    "postgres_dsn": "postgres://vurarad:secret@postgres:5432/vurarad",
    "oidc_issuer": "https://idp.local/vurarad",
    "oidc_audience": "vurarad-api",
    "oidc_hmac_secret": "onprem-shared-secret",
    "segmentation_region": "local",
}


def _onprem(**overrides: object) -> dict[str, object]:
    return {**_ONPREM_KWARGS, **overrides}


# ---------------------------------------------------------------------------
# Valid on-prem configuration
# ---------------------------------------------------------------------------
def test_valid_onprem_config_accepted() -> None:
    s = Settings(**_onprem())
    assert s.is_onprem is True
    assert s.hosting == "onprem"
    assert s.storage_backend == "minio"
    assert s.postgres_dsn is not None


def test_is_onprem_false_for_cloud() -> None:
    s = Settings(**{**_CLOUD_KWARGS})
    assert s.is_onprem is False
    assert s.hosting == "cloud"


def test_onprem_gcp_region_outside_allowlist_ok() -> None:
    """The Vertex allowlist check is skipped for onprem — 'local' is valid."""
    assert "local" not in VERTEX_ALLOWLIST
    s = Settings(**_onprem(gcp_region="site-a"))
    assert s.gcp_region == "site-a"


# ---------------------------------------------------------------------------
# Residency: on-prem must not call a cloud region (criterion 6)
# ---------------------------------------------------------------------------
def test_onprem_rejects_cloud_segmentation_region() -> None:
    """segmentation_region in the Vertex allowlist → rejected."""
    assert "us-central1" in VERTEX_ALLOWLIST
    with pytest.raises(ValidationError):
        Settings(**_onprem(segmentation_region="us-central1"))


def test_onprem_rejects_africa_south1_segmentation_region() -> None:
    """Even africa-south1 (a cloud region) is rejected for onprem."""
    assert "africa-south1" in VERTEX_ALLOWLIST
    with pytest.raises(ValidationError):
        Settings(**_onprem(segmentation_region="africa-south1"))


# ---------------------------------------------------------------------------
# Required dependencies
# ---------------------------------------------------------------------------
def test_onprem_requires_minio_storage() -> None:
    with pytest.raises(ValidationError):
        Settings(**_onprem(storage_backend="gcs"))


def test_onprem_requires_postgres_dsn() -> None:
    with pytest.raises(ValidationError):
        Settings(**_onprem(postgres_dsn=None))


def test_onprem_requires_oidc_issuer() -> None:
    with pytest.raises(ValidationError):
        Settings(**_onprem(oidc_issuer=None))


def test_onprem_requires_oidc_audience() -> None:
    with pytest.raises(ValidationError):
        Settings(**_onprem(oidc_audience=None))


def test_onprem_requires_key_source() -> None:
    """At least one of oidc_jwks_path or oidc_hmac_secret must be set."""
    with pytest.raises(ValidationError):
        Settings(**_onprem(oidc_jwks_path=None, oidc_hmac_secret=None))


def test_onprem_jwks_path_satisfies_key_requirement() -> None:
    """oidc_jwks_path alone (without hmac_secret) is valid."""
    s = Settings(**_onprem(oidc_hmac_secret=None, oidc_jwks_path="/etc/vurarad/jwks.json"))
    assert s.oidc_jwks_path == "/etc/vurarad/jwks.json"
    assert s.oidc_hmac_secret is None


def test_onprem_hmac_secret_satisfies_key_requirement() -> None:
    """oidc_hmac_secret alone (without jwks_path) is valid."""
    s = Settings(**_onprem(oidc_jwks_path=None))
    assert s.oidc_hmac_secret == "onprem-shared-secret"
    assert s.oidc_jwks_path is None


# ---------------------------------------------------------------------------
# Cloud tier is unaffected by on-prem requirements
# ---------------------------------------------------------------------------
def test_cloud_does_not_require_postgres_dsn() -> None:
    s = Settings(**{**_CLOUD_KWARGS})
    assert s.postgres_dsn is None
    assert s.is_onprem is False


def test_cloud_does_not_require_oidc_settings() -> None:
    s = Settings(**{**_CLOUD_KWARGS})
    assert s.oidc_issuer is None
    assert s.oidc_audience is None


def test_cloud_still_enforces_vertex_allowlist() -> None:
    """Cloud tier must still use a Vertex-allowlisted region."""
    with pytest.raises(ValidationError):
        Settings(**{**_CLOUD_KWARGS, "gcp_region": "local"})
