"""WP14 criterion 3, 4, 5 — degradation order, overage metering, no cost constants.

A radiologist mid-shift must never be unable to open, read, or sign a study
because of a billing threshold.  ``AI_DISABLED`` blocks ``ai:use`` and
``research:draft`` only; no stage below ``SUSPENDED`` blocks ``report:sign``,
``report:write``, or ``study:read``.  ``OVERAGE`` continues ingest and meters
``overageImages`` from ``images_ingested`` — metered, never estimated.  No cost
constant appears in any service module (rate cards are data files).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.billing.meters import Meter
from app.billing.rate_card import RateCard
from app.core.capabilities import Capability
from app.core.config import Settings, SpendStage
from app.repositories.usage_repo import CeilingConfig, UsagePeriod
from app.services.ceiling_service import CeilingService, blocked_capabilities

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO_ROOT / "app" / "services"

_PROTECTED = {Capability.STUDY_READ, Capability.REPORT_WRITE, Capability.REPORT_SIGN}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "gcp_project_id": "vurarad-test",
        "gcp_region": "us-central1",
        "pixel_bucket_name": "pixels",
        "audit_bucket_name": "audit",
        "firebase_project_id": "vurarad-test",
    }
    base.update(overrides)
    return Settings(**base)


def _rate_card(*, included_images: int = 1000) -> RateCard:
    # A test rate card: $1.0/study makes ratios trivial to reason about.  This
    # is a test file, not a service module, so the literal is fine here.
    return RateCard(
        region="test",
        effective_date="2026-01-01",
        version="2026-01",
        currency="USD",
        included_images_per_month=included_images,
        rates={Meter.STUDIES_INGESTED: 1.0, Meter.IMAGES_INGESTED: 0.0},
    )


class FakeUsageStore:
    """Minimal :class:`UsageStore` returning canned usage / ceiling config."""

    def __init__(
        self, *, usage: UsagePeriod | None = None, config: CeilingConfig | None = None
    ) -> None:
        self._usage = usage
        self._config = config
        self.set_calls: list[tuple[str, float | None, str, bool]] = []

    async def increment_meter(
        self, tenant_id: str, period: str, meter: Meter, delta: float, unit: object
    ) -> None:
        pass

    async def record_event(
        self, tenant_id: str, period: str, event_type: str, detail: dict[str, object]
    ) -> None:
        pass

    async def get_usage(self, tenant_id: str, period: str) -> UsagePeriod | None:
        return self._usage

    async def list_history(self, tenant_id: str, months: int) -> list[UsagePeriod]:
        return []

    async def get_ceiling_config(self, tenant_id: str) -> CeilingConfig | None:
        return self._config

    async def set_ceiling_config(
        self, tenant_id: str, ceiling_usd: float | None, reason: str, suspended: bool
    ) -> None:
        self.set_calls.append((tenant_id, ceiling_usd, reason, suspended))


async def _state(
    studies: float,
    *,
    images: float = 0.0,
    config: CeilingConfig | None = None,
    included_images: int = 1000,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> object:
    store = FakeUsageStore(
        usage=UsagePeriod(
            tenant_id="t1",
            period="2026-08",
            meters={"studies_ingested": studies, "images_ingested": images},
        ),
        config=config,
    )
    svc = CeilingService(
        store, _rate_card(included_images=included_images), settings or _settings()
    )
    return await svc.compute_state("t1", period="2026-08", now=now)


# ---------------------------------------------------------------------------
# Criterion 3: degradation order — blocked capabilities per stage
# ---------------------------------------------------------------------------
def test_ai_disabled_blocks_ai_and_research_draft_only() -> None:
    blocked = blocked_capabilities(SpendStage.AI_DISABLED)
    assert blocked == frozenset({Capability.AI_USE, Capability.RESEARCH_DRAFT})


def test_overage_blocks_ai_and_research_draft_only() -> None:
    blocked = blocked_capabilities(SpendStage.OVERAGE)
    assert blocked == frozenset({Capability.AI_USE, Capability.RESEARCH_DRAFT})


def test_ok_and_warn_block_nothing() -> None:
    assert blocked_capabilities(SpendStage.OK) == frozenset()
    assert blocked_capabilities(SpendStage.WARN) == frozenset()


def test_suspended_blocks_ingest_and_ai_but_not_reading_or_signing() -> None:
    blocked = blocked_capabilities(SpendStage.SUSPENDED)
    assert Capability.STUDY_IMPORT in blocked
    assert Capability.STUDY_WRITE in blocked
    assert Capability.AI_USE in blocked
    assert Capability.RESEARCH_DRAFT in blocked
    assert blocked & _PROTECTED == frozenset()


@pytest.mark.parametrize("stage", list(SpendStage))
def test_no_stage_blocks_protected_capabilities(stage: SpendStage) -> None:
    """report:sign, report:write, study:read are never spend-blocked (criterion 3)."""
    assert blocked_capabilities(stage) & _PROTECTED == frozenset()


# ---------------------------------------------------------------------------
# Criterion 3: stage transitions via cost ratio
# ---------------------------------------------------------------------------
async def test_stage_ok_below_warn_threshold() -> None:
    state = await _state(5.0)  # $5 / $10 = 0.5
    assert state.stage == SpendStage.OK  # type: ignore[attr-defined]
    assert state.blocked_capabilities == []  # type: ignore[attr-defined]


async def test_stage_warn_at_80_percent() -> None:
    state = await _state(8.0)  # $8 / $10 = 0.8
    assert state.stage == SpendStage.WARN  # type: ignore[attr-defined]


async def test_stage_ai_disabled_at_95_percent() -> None:
    state = await _state(9.5)  # $9.5 / $10 = 0.95
    assert state.stage == SpendStage.AI_DISABLED  # type: ignore[attr-defined]
    assert set(state.blocked_capabilities) == {"ai:use", "research:draft"}  # type: ignore[attr-defined]


async def test_stage_overage_at_100_percent() -> None:
    state = await _state(10.0)  # $10 / $10 = 1.0
    assert state.stage == SpendStage.OVERAGE  # type: ignore[attr-defined]


async def test_suspended_override_regardless_of_cost() -> None:
    config = CeilingConfig(ceiling_usd=None, reason="manual", suspended=True, set_at="")
    state = await _state(1.0, config=config)  # tiny cost, but suspended
    assert state.stage == SpendStage.SUSPENDED  # type: ignore[attr-defined]
    assert state.suspended is True  # type: ignore[attr-defined]


async def test_ceiling_override_used_when_set() -> None:
    config = CeilingConfig(ceiling_usd=25.0, reason="enterprise pilot", suspended=False, set_at="")
    state = await _state(10.0, config=config)  # $10 / $25 = 0.4 → OK
    assert state.stage == SpendStage.OK  # type: ignore[attr-defined]
    assert state.ceiling_usd == 25.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Criterion 4: OVERAGE continues ingest; overage metered from images_ingested
# ---------------------------------------------------------------------------
async def test_overage_images_metered_from_images_ingested() -> None:
    # 1000 images included; 1200 ingested → 200 overage images.
    state = await _state(10.0, images=1200.0, included_images=1000)
    assert state.stage == SpendStage.OVERAGE  # type: ignore[attr-defined]
    assert state.images_used == 1200  # type: ignore[attr-defined]
    assert state.overage_images == 200  # type: ignore[attr-defined]
    # 200 * $0.25/100 = $0.50 — metered from the count, never estimated.
    assert state.overage_charge_usd == 0.5  # type: ignore[attr-defined]


async def test_no_overage_within_included_images() -> None:
    state = await _state(5.0, images=500.0, included_images=1000)
    assert state.overage_images == 0  # type: ignore[attr-defined]
    assert state.overage_charge_usd == 0.0  # type: ignore[attr-defined]


def test_overage_does_not_block_ingest() -> None:
    """OVERAGE continues ingest — study:import/study:write are NOT blocked."""
    blocked = blocked_capabilities(SpendStage.OVERAGE)
    assert Capability.STUDY_IMPORT not in blocked
    assert Capability.STUDY_WRITE not in blocked


async def test_overage_charge_uses_settings_price() -> None:
    # Custom overage price $0.50/100 → 200 images = $1.00.
    settings = _settings(overage_price_per_100_images_usd=0.50)
    state = await _state(10.0, images=1200.0, included_images=1000, settings=settings)
    assert state.overage_charge_usd == 1.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Criterion 5: no cost constant in any service module; rate cards are data
# ---------------------------------------------------------------------------
# Cost literals that must never appear in a service module — they live in rate
# card YAML files or config, never in app/services/*.py.
_FORBIDDEN_COST_TOKENS: list[str] = [
    "0.0334",
    "0.0025",
    "0.023",
    "0.085",
    "0.0000015",
    "0.000004",
    "0.000005",
    "0.026",
]


def test_no_cost_constant_in_service_modules() -> None:
    """grep: no cost constant appears in any app/services/*.py module."""
    offenders: list[str] = []
    for path in sorted(SERVICES_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_COST_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert offenders == [], f"cost constants found in service modules: {offenders}"


def test_rate_cards_are_data_files_with_effective_dates() -> None:
    for region in ("africa-south1", "us-central1"):
        card = RateCard.load(region)
        assert card.effective_date  # has an effective date
        assert card.version
        assert Meter.STUDIES_INGESTED in card.rates  # per-meter costs are data
        assert card.included_images_per_month > 0


def test_rate_card_unknown_meter_rejected() -> None:
    bad = """
region: x
effective_date: "2026-01-01"
version: "2026-01"
rates:
  not_a_meter: { unit: count, unit_cost_usd: 1.0 }
"""
    with pytest.raises(ValueError):
        RateCard.from_yaml(bad, region="x")


# ---------------------------------------------------------------------------
# Config validation — degradation thresholds strictly ascending; ceiling > 0
# ---------------------------------------------------------------------------
def test_rejects_non_positive_ceiling() -> None:
    with pytest.raises(ValidationError):
        _settings(tenant_monthly_infra_ceiling_usd=0.0)


def test_rejects_unordered_thresholds() -> None:
    with pytest.raises(ValidationError):
        _settings(degradation_stage_thresholds=(0.95, 0.80, 1.00))


def test_rejects_threshold_above_one() -> None:
    with pytest.raises(ValidationError):
        _settings(degradation_stage_thresholds=(0.80, 0.95, 1.50))


def test_default_thresholds_are_valid() -> None:
    s = _settings()
    assert s.degradation_stage_thresholds == (0.80, 0.95, 1.00)


# ---------------------------------------------------------------------------
# Admin ceiling write-through + misc
# ---------------------------------------------------------------------------
async def test_set_ceiling_config_records_call() -> None:
    store = FakeUsageStore()
    await store.set_ceiling_config("t1", ceiling_usd=25.0, reason="pilot", suspended=False)
    assert store.set_calls == [("t1", 25.0, "pilot", False)]


def test_blocked_capabilities_returns_frozenset() -> None:
    result = blocked_capabilities(SpendStage.SUSPENDED)
    assert isinstance(result, frozenset)
    assert all(isinstance(c, Capability) for c in result)


def test_period_now_is_in_august_2026() -> None:
    """Sanity guard for the projection maths in compute_state."""
    assert datetime.now(UTC).year == 2026


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
