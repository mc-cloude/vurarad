"""Immutable audit event — chained via prevHash for tamper detection."""

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AuditEvent:
    """One auditable action.  seq is monotonic within the audit store; prevHash
    links to the previous event, forming an append-only chain.  hash is the
    SHA-256 of this event's ordered fields.
    """

    seq: int
    prev_hash: str  # SHA-256 hex of previous event, or "0000..." for genesis
    event_type: str
    actor: str  # user UID
    second_factor: bool
    timestamp: int = field(default_factory=lambda: int(time.time()))
    detail: dict[str, Any] = field(default_factory=dict)
    patient_key: str = ""  # SHA-256(first 16 chars) of case_uid, empty for non-patient events
    hash: str = ""  # computed after fields are set

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "seq": self.seq,
                "prev_hash": self.prev_hash,
                "event_type": self.event_type,
                "actor": self.actor,
                "second_factor": self.second_factor,
                "timestamp": self.timestamp,
                "detail": self.detail,
                "patient_key": self.patient_key,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def seal(self) -> None:
        self.hash = self.compute_hash()

    def verify_chain(self, previous: "AuditEvent") -> bool:
        """Check prev_hash links and this event's hash is consistent."""
        if self.prev_hash != previous.hash:
            return False
        return self.compute_hash() == self.hash

    @staticmethod
    def genesis() -> "AuditEvent":
        genesis = AuditEvent(
            seq=0,
            prev_hash="0" * 64,
            event_type="AUDIT_CHAIN_GENESIS",
            actor="system",
            second_factor=False,
        )
        genesis.seal()
        return genesis
