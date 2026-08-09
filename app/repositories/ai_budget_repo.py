"""AI budget repository — monthly per-tenant spend ceiling (D15).

Counters live at the logical Firestore path
``ai_budget/{tenantId}/monthly/{YYYY-MM}``.  The :class:`DocumentStore` seam is
flat ``(collection, doc_id)``, so the path is encoded as
``collection = "ai_budget_monthly"``, ``doc_id = "{tenantId}:{YYYY-MM}"``; in a
real Firestore deployment these map to a subcollection under
``ai_budget/{tenantId}``.

The budget is a **soft monthly ceiling**: :py:meth:`check_budget` is called
*before* any SDK call (acceptance criterion 12) and refuses with
``AI_BUDGET_EXCEEDED`` when the tenant's month-to-date spend reaches the limit.
:py:meth:`record_usage` is called from a ``finally`` block so an aborted stream
still books its consumed tokens (criterion 11).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.repositories.base import DocumentStore

COLLECTION = "ai_budget_monthly"


def _current_month(now: datetime | None = None) -> str:
    """Return the current ``YYYY-MM`` bucket key."""
    return (now or datetime.now(UTC)).strftime("%Y-%m")


class AiBudgetRepo:
    """Per-tenant monthly AI spend tracker backed by a :class:`DocumentStore`."""

    def __init__(
        self,
        store: DocumentStore,
        *,
        monthly_limit_usd: float = 10.0,
        cost_per_1k_tokens: float = 0.30,
    ) -> None:
        self._store = store
        self._limit = monthly_limit_usd
        self._cost_per_1k = cost_per_1k_tokens

    @staticmethod
    def _doc_id(tenant_id: str, month: str) -> str:
        return f"{tenant_id}:{month}"

    async def get_monthly_spend(self, tenant_id: str, month: str | None = None) -> float:
        """Return the tenant's month-to-date AI spend in USD."""
        bucket = month or _current_month()
        doc = await self._store.get(COLLECTION, self._doc_id(tenant_id, bucket))
        if not doc:
            return 0.0
        return float(doc.get("spend_usd", 0.0))

    async def check_budget(self, tenant_id: str) -> bool:
        """Return ``True`` if the tenant is still within this month's budget."""
        return (await self.get_monthly_spend(tenant_id)) < self._limit

    async def record_usage(self, tenant_id: str, usage: dict[str, int]) -> None:
        """Book a usage record against the tenant's monthly counter.

        ``usage`` carries the five token counts
        (``prompt_token_count``, ``candidates_token_count``,
        ``thoughts_token_count``, ``cached_content_token_count``,
        ``total_token_count``); each is accumulated and ``spend_usd`` is
        incremented by the billable token cost.  Thoughts tokens are billed
        (they are part of ``total_token_count``) and are tracked separately.
        """
        bucket = _current_month()
        doc_id = self._doc_id(tenant_id, bucket)
        doc = await self._store.get(COLLECTION, doc_id) or {}
        total_tokens = int(usage.get("total_token_count", 0) or 0)
        cost = (total_tokens / 1000.0) * self._cost_per_1k
        doc["tenant_id"] = tenant_id
        doc["month"] = bucket
        doc["spend_usd"] = float(doc.get("spend_usd", 0.0)) + cost
        doc["total_tokens"] = int(doc.get("total_tokens", 0)) + total_tokens
        for field in (
            "prompt_token_count",
            "candidates_token_count",
            "thoughts_token_count",
            "cached_content_token_count",
        ):
            doc[field] = int(doc.get(field, 0)) + int(usage.get(field, 0) or 0)
        await self._store.set(COLLECTION, doc_id, doc)


__all__ = ["AiBudgetRepo", "COLLECTION"]
