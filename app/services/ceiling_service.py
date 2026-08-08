"""Spend ceiling — staged degradation that protects clinical workflows (§3.19.3).

``CeilingState`` is computed from ``infraCostUsd / ceilingUsd`` against
``degradation_stage_thresholds``.  The degradation order is a clinical-safety
decision as much as a commercial one: AI disables first, ingest throttles last.
A radiologist mid-shift must never be unable to open, read, or sign a study
because of a billing threshold, so no stage below SUSPENDED blocks
``report:sign``, ``report:write``, or ``study:read`` — enforced structurally.

No cost constant lives in this module: per-meter unit costs come from the rate
card, the ceiling and overage price from settings, and the thresholds from
settings.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

from pydantic import Field

from app.billing.meters import Meter
from app.billing.rate_card import RateCard
from app.core.capabilities import Capability
from app.core.config import Settings, SpendStage
from app.models.common import CamelModel
from app.repositories.usage_repo import UsageStore

# Capabilities blocked per spend stage.  This is the spend gate, independent of
# RBAC: a radiologist who holds ``ai:use`` via RBAC is still blocked during
# AI_DISABLED because the ceiling disables it.
_BLOCKED_BY_STAGE: dict[SpendStage, frozenset[Capability]] = {
    SpendStage.OK: frozenset(),
    SpendStage.WARN: frozenset(),
    SpendStage.AI_DISABLED: frozenset({Capability.AI_USE, Capability.RESEARCH_DRAFT}),
    SpendStage.OVERAGE: frozenset({Capability.AI_USE, Capability.RESEARCH_DRAFT}),
    SpendStage.SUSPENDED: frozenset(
        {
            Capability.STUDY_IMPORT,
            Capability.STUDY_WRITE,
            Capability.AI_USE,
            Capability.RESEARCH_DRAFT,
        }
    ),
}

# Capabilities that must NEVER be blocked by the spend ceiling, regardless of
# stage — reading and signing are load-bearing for an in-shift radiologist.
_PROTECTED_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.STUDY_READ, Capability.REPORT_WRITE, Capability.REPORT_SIGN}
)

# Structural invariant: no stage blocks a protected capability.
for _stage, _blocked in _BLOCKED_BY_STAGE.items():
    assert _blocked & _PROTECTED_CAPABILITIES == frozenset(), (
        f"spend stage {_stage} blocks a protected capability"
    )
# AI_DISABLED blocks exactly the AI/research-draft capabilities and nothing else.
assert _BLOCKED_BY_STAGE[SpendStage.AI_DISABLED] == frozenset(
    {Capability.AI_USE, Capability.RESEARCH_DRAFT}
)


def blocked_capabilities(stage: SpendStage) -> frozenset[Capability]:
    """Return the capabilities the spend ceiling disables at ``stage``."""
    return _BLOCKED_BY_STAGE.get(stage, frozenset())


class CeilingState(CamelModel):
    """The computed spend state for one tenant/period — the /usage response body."""

    tenant_id: str
    period: str
    stage: SpendStage
    infra_cost_usd: float
    ceiling_usd: float
    ratio: float
    images_included: int
    images_used: int
    overage_images: int
    overage_charge_usd: float
    projected_month_end_usd: float
    blocked_capabilities: list[str] = Field(default_factory=list)
    suspended: bool = False


class CeilingService:
    """Computes ``CeilingState`` from metered usage, the rate card, and settings."""

    def __init__(self, store: UsageStore, rate_card: RateCard, settings: Settings) -> None:
        self._store = store
        self._rate_card = rate_card
        self._settings = settings

    async def compute_state(
        self,
        tenant_id: str,
        *,
        period: str | None = None,
        now: datetime | None = None,
    ) -> CeilingState:
        now = now or datetime.now(UTC)
        period = period or now.strftime("%Y-%m")

        usage = await self._store.get_usage(tenant_id, period)
        meters: dict[Meter, float] = (
            {Meter(k): float(v) for k, v in usage.meters.items()} if usage else {}
        )

        infra_cost = self._rate_card.total_cost(meters)

        config = await self._store.get_ceiling_config(tenant_id)
        ceiling = (
            config.ceiling_usd
            if config is not None and config.ceiling_usd is not None
            else self._settings.tenant_monthly_infra_ceiling_usd
        )
        suspended = config.suspended if config is not None else False

        ratio = infra_cost / ceiling if ceiling > 0 else float("inf")
        stage = SpendStage.SUSPENDED if suspended else self._stage_for_ratio(ratio)

        images_used = int(meters.get(Meter.IMAGES_INGESTED, 0.0))
        images_included = self._rate_card.included_images_per_month
        overage_images = max(0, images_used - images_included)
        # Overage is metered from images_ingested (criterion 4) — never
        # estimated. The price comes from settings, not a constant here.
        overage_charge = (
            overage_images * self._settings.overage_price_per_100_images_usd / 100
        )

        blocked = blocked_capabilities(stage)
        return CeilingState(
            tenant_id=tenant_id,
            period=period,
            stage=stage,
            infra_cost_usd=round(infra_cost, 6),
            ceiling_usd=ceiling,
            ratio=round(ratio, 6),
            images_included=images_included,
            images_used=images_used,
            overage_images=overage_images,
            overage_charge_usd=round(overage_charge, 6),
            projected_month_end_usd=round(self._project_month_end(infra_cost, period, now), 6),
            blocked_capabilities=sorted(cap.value for cap in blocked),
            suspended=suspended,
        )

    def _stage_for_ratio(self, ratio: float) -> SpendStage:
        warn_t, ai_t, overage_t = self._settings.degradation_stage_thresholds
        if ratio >= overage_t:
            return SpendStage.OVERAGE
        if ratio >= ai_t:
            return SpendStage.AI_DISABLED
        if ratio >= warn_t:
            return SpendStage.WARN
        return SpendStage.OK

    @staticmethod
    def _project_month_end(infra_cost: float, period: str, now: datetime) -> float:
        """Linear projection of infra cost to month end from the run rate so far."""
        year_str, month_str = period.split("-")
        year, month = int(year_str), int(month_str)
        start = datetime(year, month, 1, tzinfo=UTC)
        last_day = calendar.monthrange(year, month)[1]
        end = datetime(year, month, last_day, 23, 59, 59, tzinfo=UTC)
        total_seconds = (end - start).total_seconds()
        elapsed = (now - start).total_seconds()
        if elapsed <= 0 or total_seconds <= 0:
            return infra_cost
        return infra_cost * (total_seconds / elapsed)
