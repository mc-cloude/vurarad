"""Versioned rate cards — per-meter unit costs as data, never in code.

A rate card is a YAML file in ``app/billing/rates/{region}.yaml`` carrying an
``effective_date`` and a ``rates`` map of meter → unit cost.  Service modules
never hardcode a cost; they call ``RateCard.cost(meter, quantity)`` or
``RateCard.total_cost(meters)``.  This is the structural enforcement of WP14
acceptance criterion 5: *grep asserts no cost constant in any service module*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from app.billing.meters import Meter


@dataclass(frozen=True, slots=True)
class RateCard:
    """Per-meter unit costs for one region, as of ``effective_date``."""

    region: str
    effective_date: str
    version: str
    currency: str
    included_images_per_month: int
    rates: dict[Meter, float] = field(default_factory=dict)

    def cost(self, meter: Meter, quantity: float) -> float:
        """USD cost of ``quantity`` units of ``meter``."""
        return quantity * self.rates.get(meter, 0.0)

    def total_cost(self, meters: dict[Meter, float]) -> float:
        """Sum of per-meter costs for a usage snapshot."""
        return sum(self.cost(meter, qty) for meter, qty in meters.items())

    @classmethod
    def load(cls, region: str) -> RateCard:
        """Load the rate card for ``region`` from its YAML file."""
        path = Path(__file__).resolve().parent / "rates" / f"{region}.yaml"
        return cls.from_yaml(path.read_text(encoding="utf-8"), region=region)

    @classmethod
    def from_yaml(cls, data: str, *, region: str) -> RateCard:
        """Parse a rate card from YAML text."""
        raw = yaml.safe_load(data)
        rates_raw: dict[str, dict[str, float]] = raw.get("rates") or {}
        rates: dict[Meter, float] = {}
        for key, entry in rates_raw.items():
            # An unknown meter key is a hard error — the catalogue and the
            # rate card must stay in sync.
            meter = Meter(key)
            rates[meter] = float(entry["unit_cost_usd"])
        return cls(
            region=region,
            effective_date=str(raw["effective_date"]),
            version=str(raw["version"]),
            currency=str(raw.get("currency", "USD")),
            included_images_per_month=int(raw.get("included_images_per_month", 0)),
            rates=rates,
        )
