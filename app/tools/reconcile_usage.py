"""Monthly Cloud Billing export reconciliation (§3.19, criterion 6).

Compares the metered usage cost against the Cloud Billing export for a tenant
and period, writes a ``METERING_RECONCILED`` usage event carrying the delta,
and pages when the discrepancy exceeds the threshold (10% by default).

Usage (Cloud Run job)::

    python -m app.tools.reconcile_usage \
        --tenant-id t1 --period 2026-08 \
        --billing-cost-usd 11.40 --region africa-south1

Exit code is non-zero when the delta exceeds the threshold, so a cron / Cloud
Scheduler wrapper can page on it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass

from app.billing.meters import Meter
from app.billing.rate_card import RateCard
from app.repositories.usage_repo import UsageStore

logger = logging.getLogger("vurarad.reconcile")

# Event type written to the usage_events subcollection.
METERING_RECONCILED = "METERING_RECONCILED"
# Discrepancy percentage above which the job pages.
RECONCILIATION_THRESHOLD_PCT = 10.0


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Outcome of comparing metered cost to the billing export."""

    period: str
    metered_cost_usd: float
    billing_cost_usd: float
    delta_usd: float
    delta_pct: float
    exceeds_threshold: bool


def reconcile(
    *,
    period: str,
    metered_cost_usd: float,
    billing_cost_usd: float,
    threshold_pct: float = RECONCILIATION_THRESHOLD_PCT,
) -> ReconciliationResult:
    """Pure comparison — no I/O. ``delta`` is billing minus metered."""
    delta = billing_cost_usd - metered_cost_usd
    denominator = metered_cost_usd if metered_cost_usd > 0 else 1.0
    pct = abs(delta) / denominator * 100.0
    return ReconciliationResult(
        period=period,
        metered_cost_usd=metered_cost_usd,
        billing_cost_usd=billing_cost_usd,
        delta_usd=delta,
        delta_pct=pct,
        exceeds_threshold=pct > threshold_pct,
    )


async def write_reconciliation(
    store: UsageStore, tenant_id: str, result: ReconciliationResult
) -> None:
    """Persist the ``METERING_RECONCILED`` event and page if above threshold."""
    await store.record_event(
        tenant_id,
        result.period,
        METERING_RECONCILED,
        {
            "meteredCostUsd": result.metered_cost_usd,
            "billingCostUsd": result.billing_cost_usd,
            "deltaUsd": result.delta_usd,
            "deltaPct": result.delta_pct,
            "exceedsThreshold": result.exceeds_threshold,
        },
    )
    if result.exceeds_threshold:
        logger.error(
            "METERING_RECONCILED delta %.2f%% exceeds threshold for %s — paging",
            result.delta_pct,
            result.period,
        )


async def reconcile_tenant(
    store: UsageStore,
    rate_card: RateCard,
    tenant_id: str,
    period: str,
    billing_cost_usd: float,
) -> ReconciliationResult:
    """Reconcile one tenant/period: read metered usage, compare, persist, page."""
    usage = await store.get_usage(tenant_id, period)
    meters: dict[Meter, float] = (
        {Meter(k): float(v) for k, v in usage.meters.items()} if usage is not None else {}
    )
    metered = rate_card.total_cost(meters)
    result = reconcile(
        period=period,
        metered_cost_usd=metered,
        billing_cost_usd=billing_cost_usd,
    )
    await write_reconciliation(store, tenant_id, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile metered usage against a Cloud Billing export."
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--period", required=True, help="YYYY-MM")
    parser.add_argument("--billing-cost-usd", type=float, required=True)
    parser.add_argument("--region", default="africa-south1")
    args = parser.parse_args(argv)

    from google.cloud.firestore import AsyncClient

    from app.core.config import settings
    from app.repositories.usage_repo import UsageRepo

    rate_card = RateCard.load(args.region)
    client = AsyncClient(
        project=settings.gcp_project_id, database=settings.firestore_database
    )
    store: UsageStore = UsageRepo(client)
    result = asyncio.run(
        reconcile_tenant(
            store, rate_card, args.tenant_id, args.period, args.billing_cost_usd
        )
    )
    print(
        f"{METERING_RECONCILED} period={result.period} "
        f"delta={result.delta_usd:.2f}usd pct={result.delta_pct:.2f}% "
        f"exceeds={result.exceeds_threshold}"
    )
    return 1 if result.exceeds_threshold else 0


if __name__ == "__main__":
    sys.exit(main())
