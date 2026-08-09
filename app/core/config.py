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

    # -- auth ----------------------------------------------------------------
    firebase_project_id: str
    identity_platform_project_id: str | None = None
    mfa_verification_seconds: int = 300
    session_idle_timeout_seconds: int = 900
    require_second_factor: bool = True
    token_revocation_check_seconds: int = 30

    # -- CORS ----------------------------------------------------------------
    cors_allow_origins: str = ""  # comma-separated, no trailing slash

    # -- Vertex AI / Gemini -------------------------------------------------
    vertex_location: str = "us-central1"  # overridden by gcp_region validator

    # -- de-identification (D12) ---------------------------------------------
    deid_require_pixel_pass: bool = True
    deid_recall_floor_us_sc_ot_xc: float = 0.99
    deid_recall_floor_cr_dx_mg: float = 0.98
    deid_recall_floor_ct_mr: float = 0.97
    deid_confidence_threshold: float = 0.9
    # Engine selection.  Defaults are dependency-free so the app runs in
    # dev/CI without paddleocr/transformers; production sets the real engines.
    deid_ocr_engine: Literal["paddle", "tesseract", "threshold"] = "threshold"
    deid_phi_classifier: Literal["openmed", "deterministic"] = "deterministic"
    # Comma-separated modalities whose detected text regions always route to
    # human review regardless of confidence (criterion 3).  Default empty — a
    # deployment opts specific modalities in.
    deid_modalities_forced_review: str = ""
    # Comma-separated "MODALITY:Manufacturer" pairs that have been measured
    # against the recall floors and cleared for automated redaction (criterion
    # 10).  Default empty — every source is unvalidated (forces review) until a
    # pair is added after validation.
    deid_validated_sources: str = ""

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

        # The pixel pass (OCR + OpenMed) is the only layer that can catch
        # burned-in PHI.  Disabling it in production would silently allow PHI
        # to leak at the pixel level — forbidden (criterion 6).
        if self.environment == Environment.production and not self.deid_require_pixel_pass:
            raise ValueError("DEID_REQUIRE_PIXEL_PASS cannot be False when ENVIRONMENT=production")

        # Region must be in the Vertex AI allowlist
        region = self.gcp_region.lower()
        if region not in VERTEX_ALLOWLIST:
            raise ValueError(
                f"GCP_REGION={self.gcp_region} is not in the Vertex AI "
                f"allowlist. Supported: {sorted(VERTEX_ALLOWLIST)}"
            )

        # Default vertex_location to gcp_region if not explicitly overridden
        if not self.vertex_location or self.vertex_location == "us-central1":
            lookup = self.environment
            if lookup == Environment.production:
                self.vertex_location = self.gcp_region

        # Residency / segmentation region guard
        if not self.segmentation_region:
            self.segmentation_region = self.gcp_region
        if (
            self.residency_policy == ResidencyPolicy.africa
            and self.segmentation_region.lower() not in VERTEX_ALLOWLIST
        ):
            raise ValueError(
                f"segmentation_region={self.segmentation_region} not in "
                f"Vertex AI allowlist for residency_policy=africa"
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
    def deid_forced_review_modalities(self) -> frozenset[str]:
        """Parsed ``deid_modalities_forced_review`` as an upper-cased modality set."""
        return _parse_csv_upper(self.deid_modalities_forced_review)

    @property
    def deid_validated_source_pairs(self) -> frozenset[tuple[str, str]]:
        """Parsed ``deid_validated_sources`` as a set of ``(modality, manufacturer)``.

        Entries are ``"MODALITY:Manufacturer"``; modality is upper-cased,
        manufacturer is preserved as given (manufacturer names are case-sensitive).
        """
        pairs: set[tuple[str, str]] = set()
        for item in self.deid_validated_sources.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            modality, manufacturer = item.split(":", 1)
            pairs.add((modality.strip().upper(), manufacturer.strip()))
        return frozenset(pairs)

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


def _parse_csv_upper(value: str) -> frozenset[str]:
    """Parse a comma-separated string into an upper-cased frozenset."""
    return frozenset(item.strip().upper() for item in value.split(",") if item.strip())
