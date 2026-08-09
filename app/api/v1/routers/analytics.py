# ruff: noqa: B008
"""Analytics route — PHI-free operational dashboard.

- ``GET /analytics/dashboard`` (analytics:read) — aggregate study/report/AI
  counts, per-modality breakdown, and a compliance block.  No patient
  identifiers appear anywhere in the response, so admin (who holds
  ``analytics:read`` but zero PHI-read capabilities) is permitted.

A radiologist or viewer (who lacks ``analytics:read``) gets ``403``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.auth import get_current_user, require_capability, require_mfa
from app.core.capabilities import Capability
from app.models.admin import AnalyticsDashboard
from app.services.analytics_service import AnalyticsCounterStore, AnalyticsService

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


class InMemoryCounterStore:
    """Default in-memory counter store used when none is wired on ``app.state``.

    Production wires a Firestore-backed store; tests inject a recording fake.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    async def increment(self, counter_name: str, amount: int = 1) -> int:
        self._counters[counter_name] = self._counters.get(counter_name, 0) + amount
        return self._counters[counter_name]

    async def read(self, counter_name: str) -> int:
        return self._counters.get(counter_name, 0)

    async def read_prefix(self, prefix: str) -> dict[str, int]:
        return {
            name: value
            for name, value in self._counters.items()
            if name.startswith(prefix)
        }


async def get_analytics_counter_store(request: Request) -> AnalyticsCounterStore:
    store = getattr(request.app.state, "analytics_counter_store", None)
    if store is None:
        store = InMemoryCounterStore()
        request.app.state.analytics_counter_store = store
    return store


CounterStoreDep = Annotated[AnalyticsCounterStore, Depends(get_analytics_counter_store)]


async def get_analytics_service(store: CounterStoreDep) -> AnalyticsService:
    return AnalyticsService(store)


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]


# ---------------------------------------------------------------------------
# GET /analytics/dashboard — PHI-free operational dashboard
# ---------------------------------------------------------------------------
@router.get(
    "/dashboard",
    dependencies=[Depends(require_capability(Capability.ANALYTICS_READ))],
    response_model=AnalyticsDashboard,
)
async def get_dashboard(
    analytics_service: AnalyticsServiceDep,
) -> AnalyticsDashboard:
    return await analytics_service.build_dashboard()
