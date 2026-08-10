"""verify_audit_chain — parsing, cross-store comparison, and CLI exit codes (§5.1)."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from app.models.audit import AuditEvent
from app.tools import verify_audit_chain as vac
from app.tools.verify_audit_chain import (
    ChainVerification,
    Divergence,
    DivergenceKind,
    compare_chains,
    date_prefixes,
    parse_bucket_record,
    parse_mirror_record,
)

# Fixed "now" = 2024-08-08 00:00:00 UTC — keeps date_prefixes deterministic.
_NOW = 1_723_104_000


# ---------------------------------------------------------------------------
# Chain builders
# ---------------------------------------------------------------------------
def _build_chain(n: int, *, start_seq: int = 1) -> list[AuditEvent]:
    """A sealed, correctly-linked chain of n events (seq start_seq .. start_seq+n-1)."""
    genesis = AuditEvent.genesis()
    prev_hash = genesis.hash
    events: list[AuditEvent] = []
    seq = start_seq
    for i in range(n):
        ev = AuditEvent(
            seq=seq,
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
        seq += 1
    return events


def _to_bucket_copy(mirror: Sequence[AuditEvent]) -> list[AuditEvent]:
    """The bucket shape: same seq/hash/prev_hash, but detail omitted (§5.1)."""
    return [
        AuditEvent(
            seq=e.seq,
            prev_hash=e.prev_hash,
            event_type=e.event_type,
            actor=e.actor,
            second_factor=e.second_factor,
            timestamp=e.timestamp,
            detail={},
            patient_key=e.patient_key,
            hash=e.hash,
        )
        for e in mirror
    ]


def _async_return(events: list[AuditEvent]) -> object:
    async def _f(database: str, days: int, *, now: int | None = None) -> list[AuditEvent]:
        return events

    return _f


def _async_raise(exc: Exception) -> object:
    async def _f(database: str, days: int, *, now: int | None = None) -> list[AuditEvent]:
        raise exc

    return _f


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parse_bucket_record_nested_in_message() -> None:
    """stdout + JsonFormatter wraps the event JSON in jsonPayload.message."""
    ev = _build_chain(1)[0]
    inner = json.dumps(
        {
            "seq": ev.seq,
            "hash": ev.hash,
            "prev_hash": ev.prev_hash,
            "event_type": ev.event_type,
            "actor": ev.actor,
            "second_factor": ev.second_factor,
            "patient_key": ev.patient_key,
            "timestamp": ev.timestamp,
        }
    )
    line = json.dumps({"jsonPayload": {"message": inner, "logger": "vurarad.audit"}})
    parsed = parse_bucket_record(line)
    assert parsed is not None
    assert parsed.seq == ev.seq
    assert parsed.hash == ev.hash
    assert parsed.detail == {}  # bucket log omits detail


def test_parse_bucket_record_direct_jsonpayload() -> None:
    ev = _build_chain(1)[0]
    line = json.dumps(
        {
            "jsonPayload": {
                "seq": ev.seq,
                "hash": ev.hash,
                "prev_hash": ev.prev_hash,
                "event_type": ev.event_type,
                "actor": ev.actor,
                "second_factor": ev.second_factor,
                "patient_key": ev.patient_key,
                "timestamp": ev.timestamp,
                "detail": {"x": 1},
            }
        }
    )
    parsed = parse_bucket_record(line)
    assert parsed is not None
    assert parsed.seq == ev.seq
    assert parsed.detail == {"x": 1}


def test_parse_bucket_record_finding_is_skipped() -> None:
    """An AUDIT_CHAIN_BROKEN finding carries no seq/hash — not an audit event."""
    line = json.dumps(
        {
            "jsonPayload": {
                "event_type": "AUDIT_CHAIN_BROKEN",
                "error": {"code": "AUDIT_CHAIN_BROKEN"},
            }
        }
    )
    assert parse_bucket_record(line) is None


def test_parse_bucket_record_blank_and_garbage() -> None:
    assert parse_bucket_record("") is None
    assert parse_bucket_record("   ") is None
    assert parse_bucket_record("not json") is None


def test_parse_mirror_record_is_recomputable() -> None:
    ev = AuditEvent(
        seq=1,
        prev_hash="0" * 64,
        event_type="STUDY_VIEWED",
        actor="u1",
        second_factor=True,
        timestamp=1_000_000,
        detail={"report_id": "r1"},
    )
    ev.seal()
    doc = {
        "seq": ev.seq,
        "hash": ev.hash,
        "prev_hash": ev.prev_hash,
        "event_type": ev.event_type,
        "actor": ev.actor,
        "second_factor": ev.second_factor,
        "patient_key": ev.patient_key,
        "timestamp": ev.timestamp,
        "detail": {"report_id": "r1"},
    }
    parsed = parse_mirror_record(doc)
    assert parsed is not None
    assert parsed.seq == ev.seq
    assert parsed.detail == {"report_id": "r1"}
    assert parsed.compute_hash() == parsed.hash


def test_parse_mirror_record_missing_field_is_none() -> None:
    assert parse_mirror_record({"seq": 1}) is None


# ---------------------------------------------------------------------------
# Cross-store comparison
# ---------------------------------------------------------------------------
def test_compare_chains_empty_agree() -> None:
    v = compare_chains([], [])
    assert v.ok
    assert v.bucket_count == 0
    assert v.mirror_count == 0


def test_compare_chains_agree() -> None:
    mirror = _build_chain(4)
    bucket = _to_bucket_copy(mirror)
    v = compare_chains(bucket, mirror)
    assert v.ok
    assert v.bucket_count == 4
    assert v.mirror_count == 4


def test_compare_single_event_agrees() -> None:
    mirror = _build_chain(1)
    v = compare_chains(_to_bucket_copy(mirror), mirror)
    assert v.ok


def test_compare_tamper_mirror_deletion() -> None:
    """Bucket record missing from the mirror — the tamper case (§5.1)."""
    full = _build_chain(3)
    mirror = full[:2]  # seq 3 deleted from the mirror
    bucket = _to_bucket_copy(full)  # bucket still has seq 3
    v = compare_chains(bucket, mirror)
    assert not v.ok
    assert any(
        d.seq == 3 and d.kind == DivergenceKind.BUCKET_MISSING_FROM_MIRROR for d in v.divergences
    )


def test_compare_delivery_gap() -> None:
    """Mirror record never reached the bucket — sink delivery gap."""
    full = _build_chain(3)
    mirror = full
    bucket = _to_bucket_copy(full[:2])  # seq 3 absent from the bucket
    v = compare_chains(bucket, mirror)
    assert any(
        d.seq == 3 and d.kind == DivergenceKind.MIRROR_MISSING_FROM_BUCKET for d in v.divergences
    )


def test_compare_hash_mismatch() -> None:
    mirror = _build_chain(2)
    bucket = _to_bucket_copy(mirror)
    bucket[1].hash = "0" * 64  # one copy altered
    v = compare_chains(bucket, mirror)
    assert any(d.seq == 2 and d.kind == DivergenceKind.HASH_MISMATCH for d in v.divergences)


def test_compare_mirror_hash_invalid() -> None:
    """Tampering mirror detail after sealing makes the stored hash irreproducible."""
    mirror = _build_chain(2)
    bucket = _to_bucket_copy(mirror)
    mirror[1].detail = {"tampered": True}
    v = compare_chains(bucket, mirror)
    assert any(d.seq == 2 and d.kind == DivergenceKind.MIRROR_HASH_INVALID for d in v.divergences)


def test_compare_chain_link_broken_in_mirror() -> None:
    mirror = _build_chain(3)
    bucket = _to_bucket_copy(mirror)
    mirror[2].prev_hash = "0" * 64
    v = compare_chains(bucket, mirror)
    assert any(d.seq == 3 and d.kind == DivergenceKind.CHAIN_LINK_BROKEN for d in v.divergences)


def test_compare_chain_link_broken_in_bucket() -> None:
    mirror = _build_chain(3)
    bucket = _to_bucket_copy(mirror)
    bucket[2].prev_hash = "0" * 64
    v = compare_chains(bucket, mirror)
    assert any(d.seq == 3 and d.kind == DivergenceKind.CHAIN_LINK_BROKEN for d in v.divergences)


def test_verification_ok_property() -> None:
    v = ChainVerification(bucket_count=2, mirror_count=2)
    assert v.ok
    v.divergences.append(Divergence(DivergenceKind.HASH_MISMATCH, 1))
    assert not v.ok


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------
def test_date_prefixes() -> None:
    assert date_prefixes(3, now=_NOW) == ["2024/08/06", "2024/08/07", "2024/08/08"]


def test_date_prefixes_single_day() -> None:
    assert date_prefixes(1, now=_NOW) == ["2024/08/08"]


# ---------------------------------------------------------------------------
# CLI — main() exit codes and stdout findings
# ---------------------------------------------------------------------------
def test_main_agrees_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mirror = _build_chain(3)
    bucket = _to_bucket_copy(mirror)
    monkeypatch.setattr(vac, "read_bucket_events", lambda *a, **k: bucket)
    monkeypatch.setattr(vac, "read_mirror_events", _async_return(mirror))
    rc = vac.main(["--days", "7"])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["event_type"] == "AUDIT_CHAIN_OK"
    assert body["bucket_count"] == 3
    assert body["mirror_count"] == 3


def test_main_divergence_pages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    full = _build_chain(3)
    mirror = full[:2]  # tamper: mirror deleted seq 3
    bucket = _to_bucket_copy(full)
    monkeypatch.setattr(vac, "read_bucket_events", lambda *a, **k: bucket)
    monkeypatch.setattr(vac, "read_mirror_events", _async_return(mirror))
    rc = vac.main(["--days", "7"])
    assert rc == 1
    body = json.loads(capsys.readouterr().out)
    assert body["event_type"] == "AUDIT_CHAIN_BROKEN"
    assert body["error"]["code"] == "AUDIT_CHAIN_BROKEN"
    kinds = {d["kind"] for d in body["divergences"]}
    assert "BUCKET_MISSING_FROM_MIRROR" in kinds


def test_main_read_failure_is_broken(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(vac, "read_bucket_events", lambda *a, **k: [])
    monkeypatch.setattr(vac, "read_mirror_events", _async_raise(PermissionError("denied")))
    rc = vac.main(["--days", "7"])
    assert rc == 1
    body = json.loads(capsys.readouterr().out)
    assert body["event_type"] == "AUDIT_CHAIN_BROKEN"
    assert "store read failed" in body["reason"]


def test_main_rejects_zero_days(capsys: pytest.CaptureFixture[str]) -> None:
    rc = vac.main(["--days", "0"])
    assert rc == 1
    body = json.loads(capsys.readouterr().out)
    assert body["event_type"] == "AUDIT_CHAIN_BROKEN"
