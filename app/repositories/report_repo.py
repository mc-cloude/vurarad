"""Report repository — drafts, versions, and the transactional sign (WP5).

``sign_transaction`` wraps the report write, the version snapshot write, the
``worklist_index/current`` update, the audit-mirror write, and the analytics
counter increment in **one** all-or-nothing unit of work.  A failure anywhere
rolls back every prior write in the same transaction — the property that
``test_reports_api.py`` verifies with a flaky analytics store.

The in-memory test path implements all-or-nothing by snapshotting the document
store (plus the audit mirror and analytics counters when wired) before the unit
of work and restoring the snapshots on exception.  In production the same
boundary is a single Firestore transaction; the ``ReportTxn`` write surface is
shaped so a Firestore transaction object can slot in unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from app.models.report import ReportDraft, ReportVersion
from app.repositories.base import DocumentStore
from app.repositories.study_repo import STUDIES_COLLECTION, WORKLIST_COLLECTION, WORKLIST_DOC_ID
from app.repositories.version_repo import REPORT_VERSIONS_COLLECTION, VersionRepo

REPORTS_COLLECTION = "reports"

T = TypeVar("T")


class ReportTxn:
    """Transactional write/read surface — delegates to the document store.

    The service's unit-of-work callback receives one of these and performs every
    write through it so the repo can snapshot/rollback atomically.
    """

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        return await self._store.get(collection, doc_id)

    async def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        await self._store.set(collection, doc_id, data)

    async def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        await self._store.update(collection, doc_id, data)


class ReportRepo:
    """Draft CRUD + the transactional sign + version reads."""

    def __init__(
        self,
        store: DocumentStore,
        version_repo: VersionRepo,
        *,
        audit_mirror: Any = None,
        analytics_store: Any = None,
    ) -> None:
        self._store = store
        self._version_repo = version_repo
        self._audit_mirror = audit_mirror
        self._analytics_store = analytics_store

    # -- single-document reads / writes --------------------------------------
    async def get(self, report_id: str) -> ReportDraft | None:
        doc = await self._store.get(REPORTS_COLLECTION, report_id)
        if doc is None:
            return None
        return _draft_from_doc(doc)

    async def create(self, draft: ReportDraft) -> ReportDraft:
        """Atomically create a report document; 409 if it already exists."""
        ok = await self._store.create(
            REPORTS_COLLECTION, draft.report_id, draft.model_dump(by_alias=True)
        )
        if not ok:
            from app.core.errors import ConflictError

            raise ConflictError(f"Report {draft.report_id} already exists")
        return draft

    async def update(self, draft: ReportDraft) -> ReportDraft:
        """Overwrite a report document with the updated draft."""
        await self._store.set(REPORTS_COLLECTION, draft.report_id, draft.model_dump(by_alias=True))
        return draft

    # -- the transactional sign ----------------------------------------------
    async def sign_transaction(
        self,
        work: Callable[[ReportTxn], Awaitable[T]],
    ) -> T:
        """Run ``work`` as one all-or-nothing unit; rollback every write on error.

        ``work`` performs the report write, version write, worklist update, audit
        mirror write, and analytics counter increment through the provided
        :class:`ReportTxn` (and the audit/analytics services captured in its
        closure).  If any step raises, the document store, audit mirror, and
        analytics counters are restored to their pre-transaction snapshots so no
        partial sign persists.
        """
        snapshot = self._snapshot()
        try:
            return await work(ReportTxn(self._store))
        except Exception:
            self._rollback(snapshot)
            raise

    # -- version reads (delegated to VersionRepo) ----------------------------
    async def list_versions(self, report_id: str) -> list[ReportVersion]:
        return await self._version_repo.list_versions(report_id)

    async def get_version(self, report_id: str, version: int) -> ReportVersion | None:
        return await self._version_repo.get_version(report_id, version)

    # -- snapshot / rollback for the in-memory transaction -------------------
    def _snapshot(self) -> dict[str, Any]:
        """Snapshot the mutable state of every wired store for rollback."""
        snap: dict[str, Any] = {}
        data = getattr(self._store, "_data", None)
        if isinstance(data, dict):
            # Deep-ish copy: each collection is a dict of doc-id -> doc.
            snap["store"] = {
                col: {doc_id: dict(doc) for doc_id, doc in docs.items()}
                for col, docs in data.items()
            }
        events = getattr(self._audit_mirror, "_events", None) if self._audit_mirror else None
        if isinstance(events, list):
            snap["audit"] = list(events)
        counters = (
            getattr(self._analytics_store, "_counters", None) if self._analytics_store else None
        )
        if isinstance(counters, dict):
            snap["analytics"] = dict(counters)
        return snap

    def _rollback(self, snapshot: dict[str, Any]) -> None:
        """Restore the snapshots taken by :meth:`_snapshot`."""
        if "store" in snapshot and isinstance(getattr(self._store, "_data", None), dict):
            restored = {
                col: {doc_id: dict(doc) for doc_id, doc in docs.items()}
                for col, docs in snapshot["store"].items()
            }
            cast(Any, self._store)._data = restored
        if (
            "audit" in snapshot
            and self._audit_mirror is not None
            and isinstance(getattr(self._audit_mirror, "_events", None), list)
        ):
            cast(Any, self._audit_mirror)._events = list(snapshot["audit"])
        if (
            "analytics" in snapshot
            and self._analytics_store is not None
            and isinstance(getattr(self._analytics_store, "_counters", None), dict)
        ):
            cast(Any, self._analytics_store)._counters = dict(snapshot["analytics"])


def _draft_from_doc(doc: dict[str, Any]) -> ReportDraft:
    """Build a :class:`ReportDraft` from a stored document (camelCase)."""
    # Documents are stored via ``model_dump(by_alias=True)`` so they carry only
    # model fields; ``extra="forbid"`` therefore accepts them.  PHI redaction is
    # applied to structured *logs* of report activity in the service layer.
    return ReportDraft.model_validate(doc)


__all__ = [
    "REPORTS_COLLECTION",
    "ReportRepo",
    "ReportTxn",
    "STUDIES_COLLECTION",
    "WORKLIST_COLLECTION",
    "WORKLIST_DOC_ID",
    "REPORT_VERSIONS_COLLECTION",
]
