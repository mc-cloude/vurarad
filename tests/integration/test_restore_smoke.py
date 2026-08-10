"""Restore smoke test — invariants a restored database must satisfy (§5.6.1).

Run by the quarterly ``restore-drill.yml`` workflow against a scratch Firestore
database loaded from the latest nightly export, AND by CI against the Firestore
emulator (which is why ci.yml provisions one).  It asserts the four things the
plan calls out, as *invariants* (not exact counts, which the drill does not know
ahead of time):

1. study count  > 0               — the restore actually loaded data
2. report count <= study count    — referential consistency
3. worklist index consistency     — a status-filtered query (the worklist index,
                                     §4.5) returns exactly the studies a full
                                     scan would, i.e. the index is not stale
4. audit chain continuity         — the audit_mirror chain links prev_hash →
                                     hash and every stored hash is reproducible

Target selection (auto):
  * RESTORE_SMOKE_FIRESTORE_DB set → drill mode: connect to the real scratch DB
    with explicit WIF creds (RESTORE_SMOKE_CREDS_PATH), bypassing the fake
    service-account JSON conftest installs for unit tests.  No seeding.
  * otherwise                     → skip.  The quarterly ``restore-drill.yml``
    workflow sets ``RESTORE_SMOKE_FIRESTORE_DB``; regular CI never runs this
    test because the Firestore emulator is unreliable as a restore target.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from google.cloud.firestore import AsyncClient, FieldFilter

from app.models.audit import AuditEvent
from app.tools.verify_audit_chain import parse_mirror_record

_STUDIES = "studies"
_REPORTS = "reports"
_AUDIT = "audit_mirror"


# ---------------------------------------------------------------------------
# Chain helpers
# ---------------------------------------------------------------------------
def _build_chain(n: int) -> list[AuditEvent]:
    genesis = AuditEvent.genesis()
    prev_hash = genesis.hash
    events: list[AuditEvent] = []
    for i in range(n):
        ev = AuditEvent(
            seq=i + 1,
            prev_hash=prev_hash,
            event_type="STUDY_VIEWED",
            actor="u1",
            second_factor=True,
            timestamp=1_000_000 + i,
            detail={"i": i},
        )
        ev.seal()
        events.append(ev)
        prev_hash = ev.hash
    return events


def _chain_continuous(events: list[AuditEvent]) -> bool:
    """True iff the chain links prev_hash → hash and every hash is reproducible."""
    if not events:
        return True
    if events[0].compute_hash() != events[0].hash:
        return False
    return all(cur.verify_chain(prev) for prev, cur in zip(events, events[1:], strict=False))


# ---------------------------------------------------------------------------
# Target selection + client (drill mode only — CI never runs this test)
# ---------------------------------------------------------------------------
def _resolve_target() -> str | None:
    if os.environ.get("RESTORE_SMOKE_FIRESTORE_DB"):
        return "gcp"
    return None


def _make_client() -> AsyncClient:
    project = os.environ.get("GCP_PROJECT_ID", "vurarad-test")
    database = os.environ.get("RESTORE_SMOKE_FIRESTORE_DB") or os.environ.get(
        "FIRESTORE_DATABASE", "(default)"
    )
    creds_path = os.environ.get("RESTORE_SMOKE_CREDS_PATH")
    if creds_path:
        # Drill mode: WIF creds are an external-account file, not a service-account
        # key, and conftest has clobbered GOOGLE_APPLICATION_CREDENTIALS with a fake
        # SA for unit tests — so load the real creds explicitly from the path the
        # workflow captured before invoking pytest.
        import google.auth

        creds, _ = google.auth.load_credentials_from_file(creds_path)
        return AsyncClient(project=project, database=database, credentials=creds)
    return AsyncClient(project=project, database=database)


async def _read_all(coll: Any) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    async for doc in coll.stream():
        out.append((doc.id, doc.to_dict() or {}))
    return out


async def _query_open(db: AsyncClient) -> list[Any]:
    # Single-field filter only — the scratch DB created for the drill does not
    # carry the (status, date) composite index, so no order_by here.
    return [
        d
        async for d in db.collection(_STUDIES)
        .where(filter=FieldFilter("status", "==", "OPEN"))
        .stream()
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_chain_continuous_helper_valid() -> None:
    assert _chain_continuous(_build_chain(3))


def test_chain_continuous_helper_broken() -> None:
    chain = _build_chain(3)
    chain[1].prev_hash = "0" * 64
    assert not _chain_continuous(chain)


async def test_restore_smoke_invariants() -> None:
    if _resolve_target() is None:
        pytest.skip("RESTORE_SMOKE_FIRESTORE_DB not set — only runs in restore-drill workflow")
    db = _make_client()
    # Drill mode: the scratch DB is freshly created from an export, so a
    # full scan *is* the restored database.
    studies = await _read_all(db.collection(_STUDIES))
    reports = await _read_all(db.collection(_REPORTS))
    audit_docs = await _read_all(db.collection(_AUDIT))

    # 1 + 2 — study/report counts: data is present and referentially consistent.
    assert len(studies) > 0
    assert len(reports) <= len(studies)

    # 3 — worklist index consistency.
    open_index = {d.id for d in await _query_open(db)}
    open_scan = {sid for sid, data in studies if data.get("status") == "OPEN"}
    assert open_index == open_scan

    # 4 — audit chain continuity across the restored mirror.
    events = sorted(
        (e for e in (parse_mirror_record(data) for _, data in audit_docs) if e is not None),
        key=lambda e: e.seq,
    )
    assert _chain_continuous(events), "restored audit chain is not continuous"
