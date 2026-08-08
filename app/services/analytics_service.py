"""Analytics service — counters, chain-head storage, query.

Every business operation increments a counter through this service.  Counters
are Firestore Increment operations so a read-modify-write race can never lose
a count.  The service also stores the current audit chain head for fast seq
lookup without scanning the chain.
"""

from typing import Protocol


class AnalyticsCounterStore(Protocol):
    """Backend for incrementing counters — Firestore in production."""

    async def increment(self, counter_name: str, amount: int = 1) -> int:
        """Atomically increment and return the new value."""
        ...

    async def read(self, counter_name: str) -> int:
        """Read current value."""
        ...


class AnalyticsService:
    """High-level counters for studies, reports, users, and AI usage."""

    def __init__(self, store: AnalyticsCounterStore) -> None:
        self._store = store

    async def study_accessed(self) -> int:
        return await self._store.increment("studies_accessed")

    async def report_created(self) -> int:
        return await self._store.increment("reports_created")

    async def report_signed(self) -> int:
        return await self._store.increment("reports_signed")

    async def ai_request(self) -> int:
        return await self._store.increment("ai_requests")

    async def images_ingested(self, count: int) -> int:
        return await self._store.increment("images_ingested", count)

    async def images_viewed(self, count: int) -> int:
        return await self._store.increment("images_viewed", count)

    async def audit_chain_length(self) -> int:
        return await self._store.read("audit_chain_length")

    async def increment_audit_chain_length(self) -> int:
        return await self._store.increment("audit_chain_length")
