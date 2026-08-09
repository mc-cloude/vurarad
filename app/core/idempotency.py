"""Idempotency-key support — (uid, key) records with request-hash comparison.

Idempotency is enforced for state-changing operations (POST, PATCH, DELETE).
Per §3.13: keys live 24h, duplicate keys with identical bodies return the
original response; keys with different bodies return 409 IDEMPOTENCY_MISMATCH.
"""

import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class IdempotencyRecord:
    uid: str  # actor UID
    key: str  # Idempotency-Key header
    request_hash: str
    response_body: str
    status_code: int
    created_at: float = field(default_factory=time.time)
    expire_at: float = 0.0

    def __post_init__(self) -> None:
        if self.expire_at == 0.0:
            self.expire_at = self.created_at + 86400  # 24 h

    @property
    def expired(self) -> bool:
        return time.time() > self.expire_at


class IdempotencyStore:
    """In-memory idempotency store for WP1.

    In production this is backed by a Firestore collection with a TTL policy.
    """

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}

    def _make_key(self, uid: str, idem_key: str) -> str:
        return f"{uid}:{idem_key}"

    @staticmethod
    def _hash_request(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def check_or_record(
        self,
        uid: str,
        idem_key: str,
        request_body: bytes,
        response_body: str,
        status_code: int,
    ) -> tuple[bool, str]:
        """Returns (is_new, conflict_reason_or_empty)."""
        composite = self._make_key(uid, idem_key)
        request_hash = self._hash_request(request_body)

        existing = self._records.get(composite)
        if existing and not existing.expired:
            if existing.request_hash == request_hash:
                return True, ""  # replay OK
            return False, "IDEMPOTENCY_MISMATCH"

        self._records[composite] = IdempotencyRecord(
            uid=uid,
            key=idem_key,
            request_hash=request_hash,
            response_body=response_body,
            status_code=status_code,
        )
        return True, ""
