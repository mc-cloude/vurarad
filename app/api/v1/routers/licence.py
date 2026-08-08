"""Licence and billing-admin endpoints (§3.19.2, §3.19.4).

``GET /api/v1/licence`` returns the current licence status (any authenticated
user).  The admin endpoints require ``billing:read`` (read usage) or
``billing:manage`` (set ceiling / issue licence); ``billing:manage`` routes
also enforce a fresh second factor via ``require_mfa``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core.auth import AuthenticatedUser, get_current_user, require_capability, require_mfa
from app.core.capabilities import Capability
from app.models.common import CamelModel
from app.repositories.usage_repo import UsageStore
from app.services.ceiling_service import CeilingService
from app.services.licence_service import LicenceService

router = APIRouter(tags=["licence"])


def _licence_service(request: Request) -> LicenceService:
    svc: LicenceService = request.app.state.licence_service
    return svc


def _ceiling_service(request: Request) -> CeilingService:
    svc: CeilingService = request.app.state.ceiling_service
    return svc


def _usage_store(request: Request) -> UsageStore:
    store: UsageStore = request.app.state.usage_store
    return store


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class CeilingBody(CamelModel):
    ceiling_usd: float | None = None
    reason: str = ""
    suspended: bool = False


class LicenceBody(CamelModel):
    licence_token: str


# ---------------------------------------------------------------------------
# Dependency aliases
# ---------------------------------------------------------------------------
_Authed = Annotated[AuthenticatedUser, Depends(get_current_user)]
_BillingRead = Annotated[AuthenticatedUser, Depends(require_capability(Capability.BILLING_READ))]
_BillingManage = Annotated[
    AuthenticatedUser, Depends(require_capability(Capability.BILLING_MANAGE))
]
_FreshMfa = Annotated[AuthenticatedUser, Depends(require_mfa)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/licence")
async def get_licence(
    request: Request,
    user: _Authed,
) -> dict[str, Any]:
    """Current licence status (capability: authenticated)."""
    del user
    svc = _licence_service(request)
    state = await svc.current_state()
    return state.model_dump(by_alias=True)


@router.get("/admin/tenants/{tenantId}/usage")
async def admin_read_usage(
    request: Request,
    tenant_id: Annotated[str, Path(alias="tenantId")],
    user: _BillingRead,
    period: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Admin: read a tenant's usage and ceiling state (capability: billing:read)."""
    del user
    svc = _ceiling_service(request)
    state = await svc.compute_state(tenant_id, period=period)
    return state.model_dump(by_alias=True)


@router.post("/admin/tenants/{tenantId}/ceiling")
async def admin_set_ceiling(
    request: Request,
    tenant_id: Annotated[str, Path(alias="tenantId")],
    body: CeilingBody,
    user: _BillingManage,
    mfa_user: _FreshMfa,
) -> dict[str, Any]:
    """Admin: set a tenant ceiling / suspension (capability: billing:manage, fresh 2FA)."""
    del user, mfa_user
    store = _usage_store(request)
    await store.set_ceiling_config(
        tenant_id,
        ceiling_usd=body.ceiling_usd,
        reason=body.reason,
        suspended=body.suspended,
    )
    svc = _ceiling_service(request)
    state = await svc.compute_state(tenant_id)
    return state.model_dump(by_alias=True)


@router.post("/admin/licence")
async def admin_issue_licence(
    request: Request,
    body: LicenceBody,
    user: _BillingManage,
    mfa_user: _FreshMfa,
) -> dict[str, Any]:
    """Admin: install a detached-JWS licence (capability: billing:manage, fresh 2FA)."""
    del user, mfa_user
    svc = _licence_service(request)
    claims = await svc.install(body.licence_token)
    return {
        "siteId": claims.site_id,
        "seats": claims.seats,
        "tier": claims.tier,
        "features": claims.features,
        "notBefore": claims.not_before,
        "notAfter": claims.not_after,
    }
