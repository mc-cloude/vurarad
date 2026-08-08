"""Usage accounting repository — ``tenants/{id}/usage/{period}`` + events.

The high-volume path is a single period document per tenant per month, updated
by atomic increment so the metering flush is bounded to one write per meter
per flush interval.  The ``usage_events`` subcollection holds rare billing
events (ceiling changes, overage transitions, reconciliation) — never one
event per metered unit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from google.cloud.firestore import AsyncClient, Increment, Query
from pydantic import Field

from app.billing.meters import Meter, MeterUnit
from app.models.common import CamelModel


# ---------------------------------------------------------------------------
# Persisted shapes
# ---------------------------------------------------------------------------
class UsagePeriod(CamelModel):
    """A tenant's metered usage for one ``YYYY-MM`` period."""

    tenant_id: str
    period: str
    meters: dict[str, float] = Field(default_factory=dict)
    infra_cost_usd: float = 0.0
    last_flushed_at: str | None = None


class CeilingConfig(CamelModel):
    """A tenant's ceiling override + manual suspension flag."""

    ceiling_usd: float | None = None
    reason: str = ""
    suspended: bool = False
    set_at: str = ""


# ---------------------------------------------------------------------------
# Store protocol — the metering and ceiling services depend on this, so tests
# inject an in-memory implementation.
# ---------------------------------------------------------------------------
class UsageStore(Protocol):
    """Persistence boundary for usage accounting."""

    async def increment_meter(
        self, tenant_id: str, period: str, meter: Meter, delta: float, unit: MeterUnit
    ) -> None: ...

    async def record_event(
        self, tenant_id: str, period: str, event_type: str, detail: dict[str, Any]
    ) -> None: ...

    async def get_usage(self, tenant_id: str, period: str) -> UsagePeriod | None: ...

    async def list_history(self, tenant_id: str, months: int) -> list[UsagePeriod]: ...

    async def get_ceiling_config(self, tenant_id: str) -> CeilingConfig | None: ...

    async def set_ceiling_config(
        self, tenant_id: str, ceiling_usd: float | None, reason: str, suspended: bool
    ) -> None: ...


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Firestore implementation
# ---------------------------------------------------------------------------
class UsageRepo:
    """Firestore-backed :class:`UsageStore`.

    Layout::

        tenants/{tenantId}/usage/{period}            ← period doc (incremented)
        tenants/{tenantId}/usage/{period}/usage_events/{auto}
        tenants/{tenantId}/ceiling/current           ← override + suspended
    """

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    def _usage_doc(self, tenant_id: str, period: str) -> Any:
        return (
            self._client.collection("tenants")
            .document(tenant_id)
            .collection("usage")
            .document(period)
        )

    async def increment_meter(
        self, tenant_id: str, period: str, meter: Meter, delta: float, unit: MeterUnit
    ) -> None:
        # One atomic write per meter per flush — `set(merge=True)` with an
        # Increment sentinel creates the doc if missing and increments
        # otherwise, so the first image and the 10,000th cost the same.
        await self._usage_doc(tenant_id, period).set(
            {
                "tenantId": tenant_id,
                "period": period,
                "meters": {meter.value: Increment(delta)},
                "units": {meter.value: unit.value},
                "lastFlushedAt": _now_iso(),
            },
            merge=True,
        )

    async def record_event(
        self, tenant_id: str, period: str, event_type: str, detail: dict[str, Any]
    ) -> None:
        ref = self._usage_doc(tenant_id, period).collection("usage_events").document()
        await ref.set({"eventType": event_type, "detail": detail, "at": _now_iso()})

    async def get_usage(self, tenant_id: str, period: str) -> UsagePeriod | None:
        snap = await self._usage_doc(tenant_id, period).get()
        if not snap.exists:
            return None
        data: dict[str, Any] = snap.to_dict() or {}
        raw_meters: dict[str, Any] = data.get("meters") or {}
        meters = {str(k): float(v) for k, v in raw_meters.items()}
        return UsagePeriod(
            tenant_id=tenant_id,
            period=period,
            meters=meters,
            infra_cost_usd=float(data.get("infraCostUsd", 0.0)),
            last_flushed_at=data.get("lastFlushedAt"),
        )

    async def list_history(self, tenant_id: str, months: int) -> list[UsagePeriod]:
        col = (
            self._client.collection("tenants")
            .document(tenant_id)
            .collection("usage")
            .order_by("period", direction=Query.DESCENDING)
            .limit(months)
        )
        snaps = await col.get()
        results: list[UsagePeriod] = []
        for snap in snaps:
            if not snap.exists:
                continue
            data = snap.to_dict() or {}
            raw_meters = data.get("meters") or {}
            results.append(
                UsagePeriod(
                    tenant_id=tenant_id,
                    period=str(data.get("period", snap.id)),
                    meters={str(k): float(v) for k, v in raw_meters.items()},
                    infra_cost_usd=float(data.get("infraCostUsd", 0.0)),
                    last_flushed_at=data.get("lastFlushedAt"),
                )
            )
        return results

    async def get_ceiling_config(self, tenant_id: str) -> CeilingConfig | None:
        snap = (
            await self._client.collection("tenants")
            .document(tenant_id)
            .collection("ceiling")
            .document("current")
            .get()
        )
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        ceiling = data.get("ceilingUsd")
        return CeilingConfig(
            ceiling_usd=float(ceiling) if ceiling is not None else None,
            reason=str(data.get("reason", "")),
            suspended=bool(data.get("suspended", False)),
            set_at=str(data.get("setAt", "")),
        )

    async def set_ceiling_config(
        self, tenant_id: str, ceiling_usd: float | None, reason: str, suspended: bool
    ) -> None:
        await (
            self._client.collection("tenants")
            .document(tenant_id)
            .collection("ceiling")
            .document("current")
            .set(
                {
                    "ceilingUsd": ceiling_usd,
                    "reason": reason,
                    "suspended": suspended,
                    "setAt": _now_iso(),
                },
                merge=True,
            )
        )


# ---------------------------------------------------------------------------
# Licence token storage — a single platform-level config document.
# ---------------------------------------------------------------------------
class FirestoreLicenceStore:
    """Stores the installed licence token at ``config/licence``."""

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def get(self) -> str | None:
        snap = await self._client.collection("config").document("licence").get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        token = data.get("token")
        return str(token) if token is not None else None

    async def set(self, token: str) -> None:
        await self._client.collection("config").document("licence").set({"token": token})
