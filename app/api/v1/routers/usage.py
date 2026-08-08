"""Usage and ceiling endpoints (§3.19.2).

``GET /api/v1/usage`` and ``GET /api/v1/usage/history`` return the caller's
metered usage and computed ceiling state.  Both require ``usage:read``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from app.core.auth import AuthenticatedUser, require_capability
from app.core.capabilities import Capability
from app.repositories.usage_repo import UsageStore
from app.services.ceiling_service import CeilingService

router = APIRouter(tags=["usage"])


def _ceiling_service(request: Request) -> CeilingService:
    svc: CeilingService = request.app.state.ceiling_service
    return svc


def _usage_store(request: Request) -> UsageStore:
    store: UsageStore = request.app.state.usage_store
    return store


_UsageCap = Annotated[AuthenticatedUser, Depends(require_capability(Capability.USAGE_READ))]


@router.get("/usage")
async def get_usage(
    request: Request,
    tenant_id: Annotated[str, Query(alias="tenantId")],
    user: _UsageCap,
    period: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Current usage + ceiling state for a tenant (capability: usage:read)."""
    del user  # authorisation enforced by the dependency; unused in the body
    svc = _ceiling_service(request)
    store = _usage_store(request)
    period_str = period or datetime.now(UTC).strftime("%Y-%m")
    state = await svc.compute_state(tenant_id, period=period)
    usage = await store.get_usage(tenant_id, period_str)
    meters = usage.meters if usage is not None else {}
    return {
        "tenantId": state.tenant_id,
        "period": state.period,
        "meters": meters,
        "infraCostUsd": state.infra_cost_usd,
        "ceilingUsd": state.ceiling_usd,
        "ceilingState": state.stage.value,
        "imagesIncluded": state.images_included,
        "imagesUsed": state.images_used,
        "overageImages": state.overage_images,
        "overageChargeUsd": state.overage_charge_usd,
        "projectedMonthEndUsd": state.projected_month_end_usd,
        "blockedCapabilities": state.blocked_capabilities,
        "suspended": state.suspended,
    }


@router.get("/usage/history")
async def get_usage_history(
    request: Request,
    tenant_id: Annotated[str, Query(alias="tenantId")],
    user: _UsageCap,
    months: Annotated[int, Query(ge=1, le=60)] = 12,
) -> list[dict[str, Any]]:
    """Historical usage periods for a tenant (capability: usage:read)."""
    del user
    store = _usage_store(request)
    history = await store.list_history(tenant_id, months)
    return [
        {
            "tenantId": p.tenant_id,
            "period": p.period,
            "meters": p.meters,
            "infraCostUsd": p.infra_cost_usd,
        }
        for p in history
    ]
