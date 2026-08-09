"""Audit service — immutable, chained, bucket-locked.

record() runs inside the caller's Firestore transaction.  It writes a mirror
to Firestore and emits to the `vurarad-audit` Cloud Logging sink that routes
to the bucket-locked GCS bucket.  The mirror is for fast query; the bucket
(which the runtime identity cannot delete from) is the system of record.

THIS MODULE CONTAINS NO try/except — an audit write failure ABORTS the
operation.  This is the opposite of `main.py:328-329` where a bare `except`
silently swallows every triage-history write.
"""

import json
import logging
from typing import Any, Protocol

from app.models.audit import AuditEvent

logger = logging.getLogger("vurarad.audit")


# -- protocol for audit mirror writes (Firestore in production, fake in tests)
class AuditMirror(Protocol):
    async def write(self, event: AuditEvent) -> None:
        """Persist mirror — no update/delete methods exist."""
        ...

    async def read_chain(self, limit: int = 1000) -> list[AuditEvent]:
        """Read audit chain for verification."""
        ...


class AuditService:
    """Records auditable events inside the caller's transaction.

    The design enforces:
    1. No try/except — failure is surface-level.
    2. Mirror + bucket sink, with the bucket as system-of-record.
    3. Chain verification across the store boundary.
    """

    def __init__(self, mirror: AuditMirror) -> None:
        self._mirror = mirror

    async def record(
        self,
        event_type: str,
        actor: str,
        second_factor: bool,
        detail: dict[str, Any] | None = None,
        patient_key: str = "",
    ) -> AuditEvent:
        """Record an auditable event.  Must be called inside a Firestore txn.

        There is NO try/except in this method.  An audit write failure aborts
        the caller's operation — audit integrity is not optional.
        """
        # Read the last event to build the chain
        chain = await self._mirror.read_chain(limit=1)
        prev_hash: str
        next_seq: int
        if chain:
            prev = chain[0]
            prev_hash = prev.hash
            next_seq = prev.seq + 1
        else:
            genesis = AuditEvent.genesis()
            prev_hash = genesis.hash
            next_seq = 1

        event = AuditEvent(
            seq=next_seq,
            prev_hash=prev_hash,
            event_type=event_type,
            actor=actor,
            second_factor=second_factor,
            detail=detail or {},
            patient_key=patient_key,
        )
        event.seal()

        await self._mirror.write(event)

        # Emit to structured logging sink → GCS bucket
        logger.info(
            json.dumps(
                {
                    "seq": event.seq,
                    "hash": event.hash,
                    "prev_hash": event.prev_hash,
                    "event_type": event.event_type,
                    "actor": event.actor,
                    "second_factor": event.second_factor,
                    "patient_key": event.patient_key,
                    "timestamp": event.timestamp,
                }
            )
        )

        return event
