"""Audit query service — filtered read, self-audit, chain verification.

``query_audit`` reads from the ``audit_mirror`` collection, enforces a maximum
92-day window, returns a chain-verification flag, and — critically — writes an
``AUDIT_VIEWED`` audit record capturing the exact filter parameters on every
call.  Reading the audit log is itself auditable.

``chain_verify`` compares the mirror against the locked-bucket integrity copy
(``audit_chain``) and re-checks each event's hash + prev-hash linkage.  A
mutated mirror record yields ``chainVerified: false``.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.core.errors import AuditWindowTooWideError
from app.models.admin import AuditEntry, AuditFilter, AuditQueryResponse
from app.models.audit import AuditEvent
from app.services.audit_service import AuditService

# Maximum audit query window — 92 days in seconds.
MAX_AUDIT_WINDOW_SECONDS = 92 * 86400

AUDIT_MIRROR_COLLECTION = "audit_mirror"
AUDIT_CHAIN_COLLECTION = "audit_chain"


# ---------------------------------------------------------------------------
# Read store protocol — Firestore in production, in-memory fake in tests
# ---------------------------------------------------------------------------
class AuditReadStore(Protocol):
    """Backend for audit reads + chain verification."""

    async def query_events(
        self, filters: AuditFilter
    ) -> tuple[list[AuditEvent], str | None]: ...

    async def count_events(self, filters: AuditFilter) -> int: ...

    async def read_mirror_chain(self, limit: int = 5000) -> list[AuditEvent]: ...

    async def read_bucket_chain(self, limit: int = 5000) -> list[AuditEvent]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _event_from_doc(doc: dict[str, Any]) -> AuditEvent:
    """Reconstruct an :class:`AuditEvent` from a stored document."""
    return AuditEvent(
        seq=int(doc.get("seq", 0)),
        prev_hash=str(doc.get("prev_hash", "")),
        event_type=str(doc.get("event_type", "")),
        actor=str(doc.get("actor", "")),
        second_factor=bool(doc.get("second_factor", False)),
        timestamp=int(doc.get("timestamp", 0)),
        detail=dict(doc.get("detail", {}) or {}),
        patient_key=str(doc.get("patient_key", "")),
        hash=str(doc.get("hash", "")),
    )


def _matches(event: AuditEvent, filters: AuditFilter) -> bool:
    if filters.from_ is not None and event.timestamp < filters.from_:
        return False
    if filters.to is not None and event.timestamp > filters.to:
        return False
    if filters.actor is not None and event.actor != filters.actor:
        return False
    if filters.action is not None and event.event_type != filters.action:
        return False
    if filters.patient_key is not None and event.patient_key != filters.patient_key:
        return False
    if filters.study_id is not None:
        return event.detail.get("studyId") == filters.study_id
    return True


# ---------------------------------------------------------------------------
# Firestore implementation
# ---------------------------------------------------------------------------
class FirestoreAuditReadStore:
    """Production :class:`AuditReadStore` backed by Firestore.

    The mirror is the fast-query copy (``audit_mirror``); the integrity copy
    (``audit_chain``) is the locked-bucket surrogate that ``chain_verify``
    compares against.
    """

    def __init__(self, doc_store: Any) -> None:
        self._store = doc_store

    async def _fetch(self, collection: str, filters: AuditFilter) -> list[AuditEvent]:
        where: list[tuple[str, str, Any]] = []
        if filters.actor is not None:
            where.append(("actor", "==", filters.actor))
        if filters.action is not None:
            where.append(("event_type", "==", filters.action))
        if filters.patient_key is not None:
            where.append(("patient_key", "==", filters.patient_key))
        rows = await self._store.query(
            collection, where=where or None, limit=5000
        )
        events = [_event_from_doc(doc) for _doc_id, doc in rows]
        return [e for e in events if _matches(e, filters)]

    async def query_events(
        self, filters: AuditFilter
    ) -> tuple[list[AuditEvent], str | None]:
        events = await self._fetch(AUDIT_MIRROR_COLLECTION, filters)
        events.sort(key=lambda e: e.seq)
        offset = int(filters.page_token) if filters.page_token else 0
        page = events[offset : offset + filters.limit]
        next_token = (
            str(offset + len(page))
            if offset + len(page) < len(events)
            else None
        )
        return page, next_token

    async def count_events(self, filters: AuditFilter) -> int:
        return len(await self._fetch(AUDIT_MIRROR_COLLECTION, filters))

    async def read_mirror_chain(self, limit: int = 5000) -> list[AuditEvent]:
        rows = await self._store.query(AUDIT_MIRROR_COLLECTION, limit=limit)
        return [_event_from_doc(doc) for _doc_id, doc in rows]

    async def read_bucket_chain(self, limit: int = 5000) -> list[AuditEvent]:
        rows = await self._store.query(AUDIT_CHAIN_COLLECTION, limit=limit)
        return [_event_from_doc(doc) for _doc_id, doc in rows]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class AuditQueryService:
    """Filtered audit read with mandatory self-audit and chain verification."""

    def __init__(
        self,
        read_store: AuditReadStore,
        audit_service: AuditService,
    ) -> None:
        self._read_store = read_store
        self._audit = audit_service

    @staticmethod
    def _to_entry(event: AuditEvent) -> AuditEntry:
        return AuditEntry(
            event_id=str(event.seq),
            timestamp=event.timestamp,
            actor=event.actor,
            action=event.event_type,
            resource=str(event.detail.get("resource", "")),
            patient_key=event.patient_key,
            study_id=str(event.detail.get("studyId", "")),
            details=dict(event.detail),
        )

    async def chain_verify(self) -> bool:
        """Compare the mirror against the locked-bucket integrity copy.

        Returns ``False`` if any mirror record's hash is internally
        inconsistent, if a chain link is broken, or if a mirror record has no
        matching (same seq + same hash) bucket record.
        """
        mirror = await self._read_store.read_mirror_chain()
        bucket = await self._read_store.read_bucket_chain()
        if not mirror:
            return not bucket
        bucket_by_seq = {e.seq: e for e in bucket}
        mirror_sorted = sorted(mirror, key=lambda e: e.seq)
        genesis = AuditEvent.genesis()
        prev_hash = "0" * 64 if mirror_sorted[0].seq == 0 else genesis.hash
        for event in mirror_sorted:
            if event.compute_hash() != event.hash:
                return False
            if event.prev_hash != prev_hash:
                return False
            bucket_event = bucket_by_seq.get(event.seq)
            if bucket_event is None or bucket_event.hash != event.hash:
                return False
            prev_hash = event.hash
        return True

    async def query_audit(
        self,
        filters: AuditFilter,
        *,
        actor: str,
        second_factor: bool,
    ) -> AuditQueryResponse:
        if filters.from_ is None or filters.to is None:
            from app.core.errors import SearchFilterRequiredError

            raise SearchFilterRequiredError("Audit query requires from and to bounds")
        window = filters.to - filters.from_
        if window < 0 or window > MAX_AUDIT_WINDOW_SECONDS:
            raise AuditWindowTooWideError(
                "Audit query window must be between 0 and 92 days"
            )

        events, next_token = await self._read_store.query_events(filters)
        total = await self._read_store.count_events(filters)
        chain_ok = await self.chain_verify()
        entries = [self._to_entry(e) for e in events]

        # Every audit read is itself audited — capture the filter parameters.
        await self._audit.record(
            "AUDIT_VIEWED",
            actor=actor,
            second_factor=second_factor,
            detail={
                "from": filters.from_,
                "to": filters.to,
                "actorFilter": filters.actor,
                "action": filters.action,
                "patientKey": filters.patient_key,
                "studyId": filters.study_id,
                "limit": filters.limit,
            },
            patient_key=filters.patient_key or "",
        )

        return AuditQueryResponse(
            entries=entries,
            next_page_token=next_token,
            total_count=total,
            chain_verified=chain_ok,
        )
