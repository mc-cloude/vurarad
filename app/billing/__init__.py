"""Billing and metering — meter catalogue, rate cards, cost accounting.

A cost number is never hardcoded in a service module.  Per-meter unit costs
live in versioned rate card YAML files under ``app/billing/rates/`` and are
loaded by :class:`app.billing.rate_card.RateCard`.
"""
