"""WP14 criterion 1 & 2 — metering writes are bounded; flush on interval/shutdown/SIGTERM.

Ingesting 10,000 images must produce far fewer than 50 Firestore writes: the
metering service accumulates in-process and flushes one write per meter per
flush interval, not one per image.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime

import pytest

from app.billing.meters import Meter, MeterUnit
from app.repositories.usage_repo import UsagePeriod
from app.services.metering_service import MeteringService, period_for


# ---------------------------------------------------------------------------
# In-memory UsageStore — counts writes the way Firestore would
# ---------------------------------------------------------------------------
class InMemoryUsageStore:
    """Fake :class:`UsageStore` — every ``increment_meter`` is one write."""

    def __init__(self) -> None:
        self._meters: dict[tuple[str, str, str], float] = {}
        self.write_count = 0
        self.event_count = 0

    async def increment_meter(
        self, tenant_id: str, period: str, meter: Meter, delta: float, unit: MeterUnit
    ) -> None:
        key = (tenant_id, period, meter.value)
        self._meters[key] = self._meters.get(key, 0.0) + delta
        self.write_count += 1

    async def record_event(
        self, tenant_id: str, period: str, event_type: str, detail: dict[str, object]
    ) -> None:
        self.event_count += 1

    async def get_usage(self, tenant_id: str, period: str) -> UsagePeriod:
        meters = {m: v for (t, p, m), v in self._meters.items() if t == tenant_id and p == period}
        return UsagePeriod(tenant_id=tenant_id, period=period, meters=meters)

    async def list_history(self, tenant_id: str, months: int) -> list[UsagePeriod]:
        return []

    async def get_ceiling_config(self, tenant_id: str) -> None:
        return None

    async def set_ceiling_config(
        self, tenant_id: str, ceiling_usd: float | None, reason: str, suspended: bool
    ) -> None:
        pass


# ---------------------------------------------------------------------------
# Criterion 1: 10,000 images → under 50 writes
# ---------------------------------------------------------------------------
async def test_ten_thousand_images_under_fifty_writes() -> None:
    store = InMemoryUsageStore()
    service = MeteringService(store, flush_interval_seconds=999.0)

    for _ in range(10_000):
        await service.record("tenant-a", Meter.IMAGES_INGESTED, 1)

    # No flush yet → zero writes despite 10,000 records.
    assert store.write_count == 0

    await service.flush()
    # One meter, one flush → exactly one write. Well under 50.
    assert store.write_count == 1
    assert store.write_count < 50

    usage = await store.get_usage("tenant-a", period_for(datetime.now(UTC)))
    assert usage.meters["images_ingested"] == 10_000


async def test_one_write_per_meter_per_flush() -> None:
    store = InMemoryUsageStore()
    service = MeteringService(store, flush_interval_seconds=999.0)

    # Many records across several meters.
    for _ in range(1_000):
        await service.record("t", Meter.IMAGES_INGESTED, 1)
        await service.record("t", Meter.DICOMWEB_QIDO, 1)
        await service.record("t", Meter.AI_DRAFT_TOKENS, 10)

    await service.flush()
    # Three distinct meters → three writes, regardless of 3,000 record() calls.
    assert store.write_count == 3


async def test_multiple_flushes_stay_bounded() -> None:
    store = InMemoryUsageStore()
    service = MeteringService(store, flush_interval_seconds=999.0)

    for _ in range(10_000):
        await service.record("t", Meter.IMAGES_INGESTED, 1)
    await service.flush()
    for _ in range(10_000):
        await service.record("t", Meter.IMAGES_INGESTED, 1)
    await service.flush()
    # Two flushes × one meter = two writes for 20,000 images.
    assert store.write_count == 2
    assert store.write_count < 50


# ---------------------------------------------------------------------------
# Criterion 2: flush on lifespan shutdown (stop) and on SIGTERM
# ---------------------------------------------------------------------------
async def test_stop_drains_residual_buffer() -> None:
    store = InMemoryUsageStore()
    service = MeteringService(store, flush_interval_seconds=999.0)
    await service.start()

    await service.record("t", Meter.IMAGES_INGESTED, 42)
    assert store.write_count == 0  # not yet flushed

    await service.stop()  # shutdown drain
    assert store.write_count == 1
    usage = await store.get_usage("t", period_for(datetime.now(UTC)))
    assert usage.meters["images_ingested"] == 42


async def test_sigterm_schedules_drain() -> None:
    """SIGTERM triggers stop(), which performs the final drain (criterion 2)."""
    store = InMemoryUsageStore()
    service = MeteringService(store, flush_interval_seconds=999.0)
    await service.start()

    await service.record("t", Meter.STUDIES_INGESTED, 7)
    assert store.write_count == 0

    # Simulate the SIGTERM handler directly — it schedules stop() as a task.
    service._on_sigterm()  # noqa: SLF001
    # Pump the loop so the scheduled drain task runs to completion.
    for _ in range(50):
        if store.write_count > 0:
            break
        await asyncio.sleep(0)
    assert store.write_count == 1

    await service.stop()


async def test_residual_loss_window_documented() -> None:
    """The residual loss window must be documented as one flush interval."""
    source = inspect.getsource(MeteringService)
    assert "one flush interval" in source
    # And never claimed to be zero.
    assert "not claimed to be zero" in source or "documented" in source


async def test_flush_is_idempotent_when_empty() -> None:
    store = InMemoryUsageStore()
    service = MeteringService(store, flush_interval_seconds=999.0)
    await service.flush()
    assert store.write_count == 0


# ---------------------------------------------------------------------------
# Record accumulates correctly across meters and tenants
# ---------------------------------------------------------------------------
async def test_record_accumulates_per_tenant_meter() -> None:
    store = InMemoryUsageStore()
    service = MeteringService(store, flush_interval_seconds=999.0)
    await service.record("t1", Meter.IMAGES_INGESTED, 5)
    await service.record("t1", Meter.IMAGES_INGESTED, 5)
    await service.record("t2", Meter.IMAGES_INGESTED, 3)
    await service.flush()
    assert store.write_count == 2  # one per (tenant, period, meter)

    u1 = await store.get_usage("t1", period_for(datetime.now(UTC)))
    u2 = await store.get_usage("t2", period_for(datetime.now(UTC)))
    assert u1.meters["images_ingested"] == 10
    assert u2.meters["images_ingested"] == 3


def test_period_for_is_utc_year_month() -> None:
    assert period_for(datetime(2026, 8, 15, 23, 30, tzinfo=UTC)) == "2026-08"


# ---------------------------------------------------------------------------
# Protocol sanity — the fake satisfies the structural contract
# ---------------------------------------------------------------------------
def test_in_memory_store_has_increment_meter() -> None:
    store = InMemoryUsageStore()
    assert hasattr(store, "increment_meter")
    assert hasattr(store, "record_event")


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
