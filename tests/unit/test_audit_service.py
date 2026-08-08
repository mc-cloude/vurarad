"""AuditService — chain building, seal/verify, NO try/except (AST assertion)."""

from __future__ import annotations

import ast
import json

import pytest

from app.models.audit import AuditEvent
from app.services.audit_service import AuditMirror, AuditService
from tests.conftest import REPO_ROOT

AUDIT_SERVICE_PATH = REPO_ROOT / "app" / "services" / "audit_service.py"


# ---------------------------------------------------------------------------
# In-memory mirror implementing the AuditMirror protocol
# ---------------------------------------------------------------------------
class InMemoryAuditMirror:
    """Fake :class:`AuditMirror` — keeps the full chain in memory.

    ``read_chain(limit=n)`` returns the LAST ``n`` events (newest first), to
    match the production contract where the chain head is queried for the
    previous hash.
    """

    def __init__(self, *, fail_on_write: bool = False) -> None:
        self.events: list[AuditEvent] = []
        self.fail_on_write = fail_on_write
        self.write_count = 0

    async def write(self, event: AuditEvent) -> None:
        if self.fail_on_write:
            raise RuntimeError("audit mirror unavailable")
        self.events.append(event)
        self.write_count += 1

    async def read_chain(self, limit: int = 1000) -> list[AuditEvent]:
        # Newest-first, limited — the service reads limit=1 for the head.
        return list(reversed(self.events))[:limit]


# ---------------------------------------------------------------------------
# Genesis / hash
# ---------------------------------------------------------------------------
def test_genesis_event_is_sealed() -> None:
    genesis = AuditEvent.genesis()
    assert genesis.seq == 0
    assert genesis.prev_hash == "0" * 64
    assert genesis.hash != ""
    assert genesis.compute_hash() == genesis.hash


def test_compute_hash_is_deterministic() -> None:
    e = AuditEvent(seq=1, prev_hash="0" * 64, event_type="X", actor="u", second_factor=True)
    assert e.compute_hash() == e.compute_hash()
    e.seal()
    assert e.hash == e.compute_hash()


def test_compute_hash_changes_with_fields() -> None:
    a = AuditEvent(seq=1, prev_hash="0" * 64, event_type="X", actor="u", second_factor=True)
    b = AuditEvent(seq=2, prev_hash="0" * 64, event_type="X", actor="u", second_factor=True)
    assert a.compute_hash() != b.compute_hash()


# ---------------------------------------------------------------------------
# Chain building
# ---------------------------------------------------------------------------
async def test_first_record_links_to_genesis() -> None:
    service = AuditService(InMemoryAuditMirror())
    event = await service.record("LOGIN", actor="u1", second_factor=True)
    genesis = AuditEvent.genesis()
    assert event.seq == 1
    assert event.prev_hash == genesis.hash
    assert event.hash != ""


async def test_chain_links_prev_hash() -> None:
    """event[n].prev_hash == event[n-1].hash (criterion #7)."""
    mirror = InMemoryAuditMirror()
    service = AuditService(mirror)
    events = [
        await service.record("A", actor="u1", second_factor=True),
        await service.record("B", actor="u1", second_factor=True),
        await service.record("C", actor="u2", second_factor=False),
    ]
    # seq is monotonic
    assert [e.seq for e in events] == [1, 2, 3]
    # each links to the previous
    for prev, cur in zip(events, events[1:], strict=False):
        assert cur.prev_hash == prev.hash
    # the full chain (oldest-first) verifies
    chain = list(mirror.events)
    for prev, cur in zip(chain, chain[1:], strict=False):
        assert cur.verify_chain(prev) is True


async def test_record_with_detail_and_patient_key() -> None:
    service = AuditService(InMemoryAuditMirror())
    event = await service.record(
        "REPORT_SIGNED",
        actor="rad1",
        second_factor=True,
        detail={"report_id": "r1"},
        patient_key="abc123",
    )
    assert event.detail == {"report_id": "r1"}
    assert event.patient_key == "abc123"
    assert event.second_factor is True


async def test_tampered_event_fails_chain_verification() -> None:
    mirror = InMemoryAuditMirror()
    service = AuditService(mirror)
    first = await service.record("A", actor="u", second_factor=True)
    second = await service.record("B", actor="u", second_factor=True)
    assert second.verify_chain(first) is True
    # Tamper with the previous event's hash — the link must break.
    object.__setattr__(first, "hash", "deadbeef")
    assert second.verify_chain(first) is False


# ---------------------------------------------------------------------------
# No try/except (criterion #6) — AST assertion
# ---------------------------------------------------------------------------
def test_audit_service_has_no_try_except() -> None:
    source = AUDIT_SERVICE_PATH.read_text()
    tree = ast.parse(source)
    offenders = [n for n in ast.walk(tree) if isinstance(n, (ast.Try, ast.ExceptHandler))]
    assert offenders == [], "audit_service.py must contain zero try/except nodes"


# ---------------------------------------------------------------------------
# Fault-injected audit failure fails the request + rolls back (criterion #6)
# ---------------------------------------------------------------------------
async def test_fault_injected_write_failure_propagates() -> None:
    """A mirror write failure must NOT be swallowed — it aborts the operation."""
    mirror = InMemoryAuditMirror(fail_on_write=True)
    service = AuditService(mirror)
    with pytest.raises(RuntimeError, match="audit mirror unavailable"):
        await service.record("A", actor="u", second_factor=True)
    # Nothing was persisted — the caller's transaction would roll back.
    assert mirror.events == []
    assert mirror.write_count == 0


# ---------------------------------------------------------------------------
# AuditMirror protocol sanity
# ---------------------------------------------------------------------------
def test_in_memory_mirror_satisfies_protocol() -> None:
    mirror: AuditMirror = InMemoryAuditMirror()
    assert hasattr(mirror, "write")
    assert hasattr(mirror, "read_chain")


def test_audit_event_serialisable_to_json() -> None:
    e = AuditEvent(seq=1, prev_hash="0" * 64, event_type="X", actor="u", second_factor=True)
    e.seal()
    payload = json.dumps(
        {
            "seq": e.seq,
            "prev_hash": e.prev_hash,
            "hash": e.hash,
            "event_type": e.event_type,
            "actor": e.actor,
        }
    )
    assert json.loads(payload)["hash"] == e.hash
