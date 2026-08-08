# ruff: noqa: B008
"""Health endpoints — no auth, no MFA, no rate limit."""

from fastapi import APIRouter, Request

from app.core.config import Settings
from app.core.deps import AuditObjectStoreDep
from app.core.errors import AuditStoreNotImmutableError

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """Liveness — always 200 if the process is running."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    request: Request,
    audit_store: AuditObjectStoreDep,
) -> dict[str, str | bool]:
    """Readiness — Firestore + settings + audit-bucket-lock probe."""
    settings: Settings = request.app.state.settings
    if not audit_store.supports_bucket_lock:
        raise AuditStoreNotImmutableError()
    return {
        "status": "ok",
        "environment": settings.environment.value,
        "region": settings.gcp_region,
        "firestore_emulator": settings.firestore_emulator_host is not None,
        "audit_store_immutable": True,
    }
