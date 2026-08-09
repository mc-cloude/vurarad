# ruff: noqa: B008
"""Reports router — offline draft sync (§3.21.4 — WP15).

- ``POST /reports`` (report:write, MFA) — create a fresh DRAFT report.
- ``GET /reports/{reportId}`` (report:read) — fetch the report state.
- ``POST /reports/{reportId}/sync`` (report:write, MFA, Idempotency-Key) — apply
  a batch of offline mutations with idempotency and conflict resolution.

Every route carries ``get_current_user`` and ``require_mfa`` at the router level
and a ``require_phi_capability`` at the route level.  Admin gets
``403 PHI_ACCESS_FORBIDDEN`` on every route because admin holds zero PHI
capabilities (B2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.v1.routers.studies_deps import require_phi_capability
from app.api.v1.routers.wp12_deps import ReportSyncServiceDep
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.services.report_sync_service import (
    CreateReportRequest,
    ReportResponse,
    SyncRequest,
    SyncResult,
)

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


def _require_idempotency_key(key: str | None) -> str:
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Idempotency-Key header is required",
                }
            },
        )
    return key


# ---------------------------------------------------------------------------
# POST /reports — create a fresh DRAFT report
# ---------------------------------------------------------------------------
@router.post(
    "",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_WRITE))],
    response_model=ReportResponse,
)
async def create_report(
    body: CreateReportRequest,
    report_sync_service: ReportSyncServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReportResponse:
    """Create a fresh DRAFT report scoped to a study (§3.21.4)."""
    return await report_sync_service.create_report(user, body.study_id, body.report_id)


# ---------------------------------------------------------------------------
# GET /reports/{reportId} — fetch the report state
# ---------------------------------------------------------------------------
@router.get(
    "/{report_id}",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_READ))],
    response_model=ReportResponse,
)
async def get_report(
    report_id: str,
    report_sync_service: ReportSyncServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReportResponse:
    """Return the report state for the client (§3.21.4)."""
    return await report_sync_service.get_report(user, report_id)


# ---------------------------------------------------------------------------
# POST /reports/{reportId}/sync — apply offline mutations
# ---------------------------------------------------------------------------
@router.post(
    "/{report_id}/sync",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_WRITE))],
    response_model=SyncResult,
)
async def sync_report(
    report_id: str,
    body: SyncRequest,
    report_sync_service: ReportSyncServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SyncResult:
    """Apply a batch of offline mutations with idempotency and conflict resolution.

    Writes a ``REPORT_SYNCED`` audit event with the applied and conflicted
    mutation IDs (criterion 9).  A ``SIGNED`` report returns
    ``409 REPORT_SIGNED`` (criterion 5).
    """
    _require_idempotency_key(idempotency_key)
    return await report_sync_service.sync(user, report_id, body)
