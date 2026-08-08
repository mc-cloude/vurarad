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
  * Firestore emulator reachable  → emulator mode (seed a dataset, verify, clean
    up).  conftest.py pops FIRESTORE_EMULATOR_HOST, so we probe localhost:8200
    and re-set it — the ci.yml ``test`` job provisions the emulator there.
  * RESTORE_SMOKE_FIRESTORE_DB set → drill mode: connect to the real scratch DB
    with explicit WIF creds (RESTORE_SMOKE_CREDS_PATH), bypassing the fake
    service-account JSON conftest installs for unit tests.  No seeding.
  * otherwise                     → skip (local dev with no emulator).
"""

from __future__ import annotations

import os
import socket
from typing import Any

import pytest
from google.cloud.firestore import AsyncClient, FieldFilter

from app.models.audit import AuditEvent
from app.tools.verify_audit_chain import parse_mirror_record

_STUDIES = "studies"
_REPORTS = "reports"
_AUDIT = "audit_mirror"

# Document ids the emulator-mode seed writes — reading only these keeps the
# test hermetic against leftover data other sessions leave in the shared
# emulator (a real restored DB contains only the restored data).
_SEED_STUDIES = ("st-smoke-1", "st-smoke-2", "st-smoke-3")
_SEED_REPORTS = ("rp-smoke-1",)
_SEED_AUDIT_IDS = tuple(f"au-smoke-{i}" for i in range(1, 5))


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


def _event_to_doc(ev: AuditEvent) -> dict[str, Any]:
    return {
        "seq": ev.seq,
        "prev_hash": ev.prev_hash,
        "event_type": ev.event_type,
        "actor": ev.actor,
        "second_factor": ev.second_factor,
        "timestamp": ev.timestamp,
        "detail": ev.detail,
        "patient_key": ev.patient_key,
        "hash": ev.hash,
    }


# ---------------------------------------------------------------------------
# Target selection + client
# ---------------------------------------------------------------------------
def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _resolve_target() -> str | None:
    if os.environ.get("FIRESTORE_EMULATOR_HOST") or _port_open("localhost", 8200):
        if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
            os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8200"
        return "emulator"
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


# ---------------------------------------------------------------------------
# Seed / read (emulator mode)
# ---------------------------------------------------------------------------
async def _seed_restored_dataset(db: AsyncClient) -> None:
    studies = [
        ("st-smoke-1", {"status": "OPEN", "patientRef": "pt-1", "date": 100, "modality": "CT"}),
        ("st-smoke-2", {"status": "OPEN", "patientRef": "pt-2", "date": 200, "modality": "MR"}),
        ("st-smoke-3", {"status": "SIGNED", "patientRef": "pt-1", "date": 300, "modality": "CR"}),
    ]
    for sid, fields in studies:
        await db.collection(_STUDIES).document(sid).set(fields)
    await (
        db.collection(_REPORTS)
        .document("rp-smoke-1")
        .set({"studyRef": "st-smoke-3", "status": "SIGNED"})
    )
    for ev in _build_chain(4):
        await db.collection(_AUDIT).document(f"au-smoke-{ev.seq}").set(_event_to_doc(ev))


async def _cleanup(db: AsyncClient) -> None:
    for sid in _SEED_STUDIES:
        await db.collection(_STUDIES).document(sid).delete()
    for rid in _SEED_REPORTS:
        await db.collection(_REPORTS).document(rid).delete()
    for aid in _SEED_AUDIT_IDS:
        await db.collection(_AUDIT).document(aid).delete()


async def _read_all(coll: Any) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    async for doc in coll.stream():
        out.append((doc.id, doc.to_dict() or {}))
    return out


async def _read_ids(
    db: AsyncClient, coll: str, ids: tuple[str, ...]
) -> list[tuple[str, dict[str, Any]]]:
    """Read only the named documents — hermetic for emulator mode."""
    out: list[tuple[str, dict[str, Any]]] = []
    for did in ids:
        snap = await db.collection(coll).document(did).get()
        if snap.exists:
            out.append((snap.id, snap.to_dict() or {}))
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
    target = _resolve_target()
    if target is None:
        pytest.skip("no Firestore emulator and no restore-drill scratch DB configured")
    db = _make_client()
    seeded = target == "emulator"
    if seeded:
        await _seed_restored_dataset(db)
    try:
        if seeded:
            # Emulator mode: verify only the dataset we just restored — a shared
            # emulator may hold leftover docs from other sessions that are not
            # part of "the restored database".
            studies = await _read_ids(db, _STUDIES, _SEED_STUDIES)
            reports = await _read_ids(db, _REPORTS, _SEED_REPORTS)
            audit_docs = await _read_ids(db, _AUDIT, _SEED_AUDIT_IDS)
            scope_ids = set(_SEED_STUDIES)
        else:
            # Drill mode: the scratch DB is freshly created from an export, so a
            # full scan *is* the restored database.
            studies = await _read_all(db.collection(_STUDIES))
            reports = await _read_all(db.collection(_REPORTS))
            audit_docs = await _read_all(db.collection(_AUDIT))
            scope_ids = None

        # 1 + 2 — study/report counts: data is present and referentially consistent.
        assert len(studies) > 0
        assert len(reports) <= len(studies)

        # 3 — worklist index consistency: the status index returns exactly the
        # studies a full scan would (no stale/missing index entries). In emulator
        # mode the comparison is scoped to the restored studies.
        open_index = {d.id for d in await _query_open(db)}
        if scope_ids is not None:
            open_index &= scope_ids
        open_scan = {sid for sid, data in studies if data.get("status") == "OPEN"}
        assert open_index == open_scan

        # 4 — audit chain continuity across the restored mirror.
        events = sorted(
            (e for e in (parse_mirror_record(data) for _, data in audit_docs) if e is not None),
            key=lambda e: e.seq,
        )
        assert _chain_continuous(events), "restored audit chain is not continuous"
    finally:
        if seeded:
            await _cleanup(db)
