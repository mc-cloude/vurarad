"""Health endpoints — no auth, no MFA, no rate limit."""

from fastapi import APIRouter, Request

from app.core.config import Settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """Liveness — always 200 if the process is running."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, str | bool]:
    """Readiness — Firestore + settings probe."""
    settings: Settings = request.app.state.settings
    return {
        "status": "ok",
        "environment": settings.environment.value,
        "region": settings.gcp_region,
        "firestore_emulator": settings.firestore_emulator_host is not None,
    }
