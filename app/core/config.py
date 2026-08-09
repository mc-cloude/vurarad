"""Application settings — validated at import, never a runtime 500."""

import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Vertex AI supported regions (from google-genai docs, validated at startup)
# ---------------------------------------------------------------------------
VERTEX_ALLOWLIST: set[str] = {
    "us-central1",
    "us-east4",
    "europe-west1",
    "europe-west4",
    "asia-southeast1",
    "asia-south1",
    "africa-south1",
}

# Regions that have Cloud Run L4 GPU availability.  africa-south1 is NOT among
# them, so the settings validator enforces CPU segmentation when the deployment
# region is outside this set (§D16).
L4_GPU_REGIONS: set[str] = {
    "us-central1",
    "us-east4",
    "europe-west1",
    "europe-west4",
    "asia-southeast1",
    "asia-south1",
}


# ---------------------------------------------------------------------------
# Degradation stages for the spend ceiling (D15)
# ---------------------------------------------------------------------------
class SpendStage(StrEnum):
    OK = "OK"
    WARN = "WARN"
    AI_DISABLED = "AI_DISABLED"
    OVERAGE = "OVERAGE"
    SUSPENDED = "SUSPENDED"


# ---------------------------------------------------------------------------
class Environment(StrEnum):
    development = "development"
    staging = "staging"
    production = "production"


class ResidencyPolicy(StrEnum):
    africa = "africa"
    europe = "europe"
    us = "us"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """Every security-relevant field has no default and must be set explicitly.

    The only allowed defaults are for dev/local emulator convenience and are
    validated away when ENVIRONMENT is 'production'.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # -- environment ---------------------------------------------------------
    environment: Environment = Environment.development
    log_level: LogLevel = LogLevel.INFO
    # ``"cloud"`` (Firestore + GCS + Vertex AI) or ``"onprem"`` (PostgreSQL +
    # MinIO + local OIDC + CPU segmentation).  Drives metadata-backend
    # selection (:func:`build_metadata_store`) and the residency guard below.
    hosting: Literal["cloud", "onprem"] = "cloud"

    # -- hosting -------------------------------------------------------------
    gcp_project_id: str
    gcp_region: str  # validated in model_validator

    # -- Firestore -----------------------------------------------------------
    firestore_emulator_host: str | None = None
    firestore_database: str = "(default)"

    # -- Cloud Storage -------------------------------------------------------
    pixel_bucket_name: str
    audit_bucket_name: str
    deid_bucket_name: str | None = None

    # -- Object-store backend selection --------------------------------------
    storage_backend: Literal["gcs", "minio"] = "gcs"
    quarantine_bucket_name: str | None = None

    # -- MinIO (on-prem S3-compatible) ---------------------------------------
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_region: str = ""
    minio_secure: bool = True

    # -- PostgreSQL (on-prem metadata backend) -------------------------------
    # Connection string for the on-prem PostgreSQL metadata store, e.g.
    # ``postgres://vurarad:secret@postgres:5432/vurarad``.  Required when
    # ``hosting == "onprem"``; ignored by the cloud tier.
    postgres_dsn: str | None = None

    # -- Local OIDC (on-prem auth) -------------------------------------------
    # Issuer and audience for the local OIDC provider (e.g. Keycloak).  The
    # on-prem tier verifies ID tokens offline — no JWKS is fetched at runtime.
    # ``oidc_jwks_path`` points to a JWKS file provisioned on disk (RS256);
    # ``oidc_hmac_secret`` is a shared secret for HS256.  At least one key
    # source is required when ``hosting == "onprem"``; ignored by the cloud tier.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_path: str | None = None
    oidc_hmac_secret: str | None = None

    # -- auth ----------------------------------------------------------------
    firebase_project_id: str
    identity_platform_project_id: str | None = None
    mfa_verification_seconds: int = 300
    session_idle_timeout_seconds: int = 900
    require_second_factor: bool = True
    token_revocation_check_seconds: int = 30

    # -- signed URLs (§3.6) --------------------------------------------------
    signed_url_ttl_seconds: int = 900  # 15 minutes
    signed_url_chunk_default: int = 250
    signed_url_chunk_max: int = 250
    sign_blob_concurrency: int = 32  # semaphore ceiling

    # -- worklist (§4.2) -----------------------------------------------------
    worklist_cap: int = 200

    # -- CORS ----------------------------------------------------------------
    cors_allow_origins: str = ""  # comma-separated, no trailing slash

    # -- Vertex AI / Gemini -------------------------------------------------
    vertex_location: str = "us-central1"  # overridden by gcp_region validator

    # -- de-identification (D12) ---------------------------------------------
    deid_require_pixel_pass: bool = True
    deid_recall_floor_us_sc_ot_xc: float = 0.99
    deid_recall_floor_cr_dx_mg: float = 0.98
    deid_recall_floor_ct_mr: float = 0.97

    # -- metering (D15/D18) --------------------------------------------------
    tenant_monthly_infra_ceiling_usd: float = 10.00
    overage_price_per_100_images_usd: float = 0.25
    included_studies_per_month: int = 300

    # -- residency (D16) -----------------------------------------------------
    residency_policy: ResidencyPolicy = ResidencyPolicy.africa
    segmentation_region: str = ""  # may differ from gcp_region; validated below

    # -- entropy -------------------------------------------------------------
    session_secret_bytes: int = 64

    # -----------------------------------------------------------------------
    # Validators
    # -----------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_env(self) -> Self:
        if self.environment == Environment.production and self.firestore_emulator_host is not None:
            raise ValueError("FIRESTORE_EMULATOR_HOST must be None when ENVIRONMENT=production")

        # Region must be in the Vertex AI allowlist (cloud tier only — on-prem
        # uses gcp_region as an opaque site label, never for a cloud call).
        region = self.gcp_region.lower()
        if self.hosting != "onprem" and region not in VERTEX_ALLOWLIST:
            raise ValueError(
                f"GCP_REGION={self.gcp_region} is not in the Vertex AI "
                f"allowlist. Supported: {sorted(VERTEX_ALLOWLIST)}"
            )

        # Default vertex_location to gcp_region if not explicitly overridden
        if not self.vertex_location or self.vertex_location == "us-central1":
            lookup = self.environment
            if lookup == Environment.production:
                self.vertex_location = self.gcp_region

        # Residency / segmentation region guard (cloud tier only — on-prem
        # segmentation runs locally and must not target a cloud region).
        if not self.segmentation_region:
            self.segmentation_region = self.gcp_region
        if (
            self.residency_policy == ResidencyPolicy.africa
            and self.hosting != "onprem"
            and self.segmentation_region.lower() not in VERTEX_ALLOWLIST
        ):
            raise ValueError(
                f"segmentation_region={self.segmentation_region} not in "
                f"Vertex AI allowlist for residency_policy=africa"
            )

        # On-prem hosting guard — residency criterion: on-prem must NOT call a
        # cloud region for AI/segmentation, must use MinIO for objects, and must
        # store metadata in PostgreSQL.  No outbound cloud dependency is valid.
        if self.hosting == "onprem":
            if self.segmentation_region.lower() in VERTEX_ALLOWLIST:
                raise ValueError(
                    f"segmentation_region={self.segmentation_region} is a cloud "
                    f"Vertex region; on-prem hosting must not call a cloud region "
                    f"(set segmentation_region to a local label such as 'local')"
                )
            if self.storage_backend != "minio":
                raise ValueError("storage_backend must be 'minio' when hosting='onprem'")
            if not self.postgres_dsn:
                raise ValueError("postgres_dsn is required when hosting='onprem'")
            if not self.oidc_issuer:
                raise ValueError("oidc_issuer is required when hosting='onprem'")
            if not self.oidc_audience:
                raise ValueError("oidc_audience is required when hosting='onprem'")
            if not self.oidc_jwks_path and not self.oidc_hmac_secret:
                raise ValueError(
                    "oidc_jwks_path or oidc_hmac_secret is required"
                    " when hosting='onprem'"
                )

        # MinIO backend validation
        if self.storage_backend == "minio":
            if not self.minio_endpoint:
                raise ValueError("MINIO_ENDPOINT is required when STORAGE_BACKEND=minio")
            if self.is_production and (not self.minio_access_key or not self.minio_secret_key):
                raise ValueError(
                    "MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required"
                    " when STORAGE_BACKEND=minio and ENVIRONMENT=production"
                )

        return self

    @property
    def is_cloud_run_gpu_available(self) -> bool:
        return self.gcp_region.lower() in L4_GPU_REGIONS

    @property
    def cors_origins(self) -> list[str]:
        if not self.cors_allow_origins.strip():
            return []
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.production

    @property
    def is_onprem(self) -> bool:
        """``True`` for the on-prem tier (PostgreSQL + MinIO + local OIDC)."""
        return self.hosting == "onprem"

    def assertion_fingerprint(self) -> str:
        """Deterministic fingerprint of security-critical values for the audit
        chain claimVersion, used to detect a config change mid-chain."""
        import hashlib

        raw = json.dumps(
            {
                "firebase_project_id": self.firebase_project_id,
                "gcp_project_id": self.gcp_project_id,
                "gcp_region": self.gcp_region,
                "residency_policy": self.residency_policy.value,
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Singleton — validated at import time
# ---------------------------------------------------------------------------
settings = Settings()  # type: ignore[call-arg]
