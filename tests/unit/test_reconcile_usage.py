"""WP14 criterion 6 — monthly reconciliation writes METERING_RECONCILED; pages > 10%."""

from __future__ import annotations

import logging

import pytest

from app.billing.meters import Meter
from app.billing.rate_card import RateCard
from app.repositories.usage_repo import UsagePeriod
from app.tools.reconcile_usage import (
    METERING_RECONCILED,
    RECONCILIATION_THRESHOLD_PCT,
    reconcile,
    reconcile_tenant,
    write_reconciliation,
)


# ---------------------------------------------------------------------------
# Pure comparison
# ---------------------------------------------------------------------------
def test_reconcile_no_delta() -> None:
    result = reconcile(period="2026-08", metered_cost_usd=10.0, billing_cost_usd=10.0)
    assert result.delta_usd == 0.0
    assert result.delta_pct == 0.0
    assert result.exceeds_threshold is False


def test_reconcile_within_threshold() -> None:
    # 10% exactly is NOT above the threshold (strictly greater than).
    result = reconcile(period="2026-08", metered_cost_usd=10.0, billing_cost_usd=11.0)
    assert result.delta_pct == 10.0
    assert result.exceeds_threshold is False


def test_reconcile_above_threshold_pages() -> None:
    result = reconcile(period="2026-08", metered_cost_usd=10.0, billing_cost_usd=11.5)
    assert result.delta_usd == 1.5
    assert result.delta_pct == 15.0
    assert result.exceeds_threshold is True


def test_reconcile_negative_delta_above_threshold() -> None:
    # Billing lower than metered by > 10% should also page.
    result = reconcile(period="2026-08", metered_cost_usd=10.0, billing_cost_usd=8.0)
    assert result.delta_usd == -2.0
    assert result.delta_pct == 20.0
    assert result.exceeds_threshold is True


def test_reconcile_zero_metered_uses_unit_denominator() -> None:
    result = reconcile(period="2026-08", metered_cost_usd=0.0, billing_cost_usd=5.0)
    assert result.exceeds_threshold is True


def test_threshold_is_ten_percent() -> None:
    assert RECONCILIATION_THRESHOLD_PCT == 10.0


# ---------------------------------------------------------------------------
# Event persistence + paging
# ---------------------------------------------------------------------------
class InMemoryUsageStore:
    def __init__(self, *, usage: UsagePeriod | None = None) -> None:
        self._usage = usage
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    async def increment_meter(
        self, tenant_id: str, period: str, meter: Meter, delta: float, unit: object
    ) -> None:
        pass

    async def record_event(
        self, tenant_id: str, period: str, event_type: str, detail: dict[str, object]
    ) -> None:
        self.events.append((tenant_id, period, event_type, detail))

    async def get_usage(self, tenant_id: str, period: str) -> UsagePeriod | None:
        return self._usage

    async def list_history(self, tenant_id: str, months: int) -> list[UsagePeriod]:
        return []

    async def get_ceiling_config(self, tenant_id: str) -> None:
        return None

    async def set_ceiling_config(
        self, tenant_id: str, ceiling_usd: float | None, reason: str, suspended: bool
    ) -> None:
        pass


async def test_write_reconciliation_records_event() -> None:
    store = InMemoryUsageStore()
    result = reconcile(period="2026-08", metered_cost_usd=10.0, billing_cost_usd=10.0)
    await write_reconciliation(store, "t1", result)
    assert len(store.events) == 1
    tenant, period, event_type, detail = store.events[0]
    assert tenant == "t1"
    assert period == "2026-08"
    assert event_type == METERING_RECONCILED
    assert detail["deltaUsd"] == 0.0
    assert detail["exceedsThreshold"] is False


async def test_write_reconciliation_pages_above_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryUsageStore()
    result = reconcile(period="2026-08", metered_cost_usd=10.0, billing_cost_usd=12.0)
    with caplog.at_level(logging.ERROR, logger="vurarad.reconcile"):
        await write_reconciliation(store, "t1", result)
    assert any("paging" in rec.message for rec in caplog.records)
    assert store.events[0][3]["exceedsThreshold"] is True


async def test_reconcile_tenant_compares_metered_to_billing() -> None:
    # Rate card: $1.0/study → 10 studies = $10 metered.  Billing says $11 → 10%.
    rate_card = RateCard(
        region="test",
        effective_date="2026-01-01",
        version="2026-01",
        currency="USD",
        included_images_per_month=0,
        rates={Meter.STUDIES_INGESTED: 1.0},
    )
    store = InMemoryUsageStore(
        usage=UsagePeriod(
            tenant_id="t1", period="2026-08", meters={"studies_ingested": 10.0}
        )
    )
    result = await reconcile_tenant(store, rate_card, "t1", "2026-08", billing_cost_usd=11.0)
    assert result.metered_cost_usd == 10.0
    assert result.delta_pct == 10.0
    assert result.exceeds_threshold is False
    assert len(store.events) == 1
    assert store.events[0][2] == METERING_RECONCILED


async def test_reconcile_tenant_no_usage_is_zero_metered() -> None:
    rate_card = RateCard(
        region="test",
        effective_date="2026-01-01",
        version="2026-01",
        currency="USD",
        included_images_per_month=0,
        rates={Meter.STUDIES_INGESTED: 1.0},
    )
    store = InMemoryUsageStore(usage=None)
    result = await reconcile_tenant(store, rate_card, "t1", "2026-08", billing_cost_usd=5.0)
    assert result.metered_cost_usd == 0.0
    assert result.exceeds_threshold is True


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
