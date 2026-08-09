"""Report-sync repository — reports, version documents, and the mutation ledger.

The mutation ledger at ``report_sync_mutations/{reportId}:{mutationId}`` is what
makes offline draft sync idempotent: a mutation is recorded here once it has been
applied, so a re-send over a flaky link is a no-op rather than a duplicate write
(criterion 4).  One version document is written per sync that applies a change
(criterion 8), and every report/version/mutation document is erased with its
study (criterion 3, PHI erasure).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.repositories.base import DocumentStore

REPORTS_COLLECTION = "reports"
REPORT_VERSIONS_COLLECTION = "report_versions"
REPORT_SYNC_MUTATIONS_COLLECTION = "report_sync_mutations"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SyncRepository:
    """Persistence for the report-sync domain: reports, versions, mutation ledger."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    # -- reports -------------------------------------------------------------
    async def get_report(self, report_id: str) -> dict[str, Any] | None:
        """Return the raw report document, or ``None`` if it does not exist."""
        return await self._store.get(REPORTS_COLLECTION, report_id)

    async def save_report(self, report_id: str, data: dict[str, Any]) -> None:
        """Create or overwrite a report document."""
        await self._store.set(REPORTS_COLLECTION, report_id, data)

    # -- mutation ledger (criterion 4) ---------------------------------------
    @staticmethod
    def _ledger_doc_id(report_id: str, mutation_id: str) -> str:
        """Per-report scoped ledger key — the idempotency record for a mutation."""
        return f"{report_id}:{mutation_id}"

    async def get_applied_mutation_ids(self, report_id: str) -> set[str]:
        """Return the set of mutationIds already applied to ``report_id``."""
        rows = await self._store.query(
            REPORT_SYNC_MUTATIONS_COLLECTION,
            where=[("report_id", "==", report_id)],
            limit=100_000,
        )
        return {str(doc.get("mutation_id", _id)) for _id, doc in rows}

    async def record_mutation(
        self,
        report_id: str,
        mutation_id: str,
        mutation_type: str,
        at: str,
    ) -> None:
        """Record that a mutation has been applied (the idempotency anchor)."""
        await self._store.set(
            REPORT_SYNC_MUTATIONS_COLLECTION,
            self._ledger_doc_id(report_id, mutation_id),
            {
                "mutation_id": mutation_id,
                "report_id": report_id,
                "type": mutation_type,
                "at": at,
                "recorded_at": _now_iso(),
            },
        )

    # -- version documents (criterion 8) -------------------------------------
    @staticmethod
    def _version_doc_id(report_id: str, version: int) -> str:
        return f"{report_id}__v{version}"

    async def record_version(
        self,
        report_id: str,
        version: int,
        applied: list[str],
        conflicted: list[str],
        actor: str,
    ) -> None:
        """Write the single version document produced by one sync."""
        await self._store.set(
            REPORT_VERSIONS_COLLECTION,
            self._version_doc_id(report_id, version),
            {
                "report_id": report_id,
                "version": version,
                "applied": applied,
                "conflicted": conflicted,
                "actor": actor,
                "at": _now_iso(),
            },
        )

    async def count_versions(self, report_id: str) -> int:
        """Return the number of version documents recorded for ``report_id``."""
        rows = await self._store.query(
            REPORT_VERSIONS_COLLECTION,
            where=[("report_id", "==", report_id)],
            limit=100_000,
        )
        return len(rows)

    # -- erasure (criterion 3 — PHI erased with the study) -------------------
    async def erase_for_study(self, study_id: str) -> int:
        """Delete every report, version, and mutation ledger entry for a study."""
        report_rows = await self._store.query(
            REPORTS_COLLECTION,
            where=[("study_id", "==", study_id)],
            limit=100_000,
        )
        report_ids = [doc_id for doc_id, _doc in report_rows]
        deleted = 0
        for report_id in report_ids:
            await self._store.delete(REPORTS_COLLECTION, report_id)
            deleted += 1
            # versions for this report
            version_rows = await self._store.query(
                REPORT_VERSIONS_COLLECTION,
                where=[("report_id", "==", report_id)],
                limit=100_000,
            )
            for doc_id, _doc in version_rows:
                await self._store.delete(REPORT_VERSIONS_COLLECTION, doc_id)
                deleted += 1
            # mutation ledger entries for this report
            mutation_rows = await self._store.query(
                REPORT_SYNC_MUTATIONS_COLLECTION,
                where=[("report_id", "==", report_id)],
                limit=100_000,
            )
            for doc_id, _doc in mutation_rows:
                await self._store.delete(REPORT_SYNC_MUTATIONS_COLLECTION, doc_id)
                deleted += 1
        return deleted


__all__ = [
    "REPORTS_COLLECTION",
    "REPORT_SYNC_MUTATIONS_COLLECTION",
    "REPORT_VERSIONS_COLLECTION",
    "SyncRepository",
]
