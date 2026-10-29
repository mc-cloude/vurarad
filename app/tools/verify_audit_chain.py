#!/usr/bin/env python3
"""Cross-store audit-chain verifier (§5.1).

Compares the bucket-locked system of record (``gs://vurarad-audit``, written by
the Cloud Logging sink under a Google-managed identity the runtime SA cannot
impersonate) against the Firestore ``audit_mirror`` (transactional, queryable,
and explicitly *deletable*) over the last N days:

* chain continuity — each event's ``prev_hash`` links to the previous event's
  ``hash``; for the mirror (which carries ``detail``) the stored hash is also
  recomputed and checked;
* cross-store agreement — every ``seq`` present in the bucket is present in the
  mirror with the same hash, and vice-versa.

A record present in the bucket but missing from the mirror is the tamper case:
the mirror was deleted but the immutable bucket copy was not.  Any divergence
emits ``AUDIT_CHAIN_BROKEN`` (§8.9) on stdout — captured by Cloud Run as
``jsonPayload`` — and exits non-zero, so the log-based alert pages and the
scheduled job is marked failed.

Run weekly as read-only ``vurarad-audit-verifier@`` via Cloud Scheduler (OIDC):

    python -m app.tools.verify_audit_chain --days 7

The verifier holds NO write permission on either store: it cannot alter the
system of record and cannot inject audit events into the bucket (§5.1).  GCP
clients are loaded through ``importlib`` so the untyped ``google.cloud.storage``
package does not require a mypy stub override.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.models.audit import AuditEvent
from app.models.errors import ErrorCode

_AUDIT_COLLECTION = "audit_mirror"
_SECONDS_PER_DAY = 86_400


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
class DivergenceKind(StrEnum):
    """How the two stores disagree (§5.1)."""

    # A bucket record with no mirror counterpart — the mirror was deleted but
    # the immutable bucket copy was not.  This is the tamper case that matters.
    BUCKET_MISSING_FROM_MIRROR = "BUCKET_MISSING_FROM_MIRROR"
    # A mirror record never reached the bucket — a sink delivery gap or a crash
    # in the <10 s async window.  Reported; still a finding after N days.
    MIRROR_MISSING_FROM_BUCKET = "MIRROR_MISSING_FROM_BUCKET"
    # Same seq, different hash between the stores — one copy was altered.
    HASH_MISMATCH = "HASH_MISMATCH"
    # prev_hash does not link to the previous event's hash within a store.
    CHAIN_LINK_BROKEN = "CHAIN_LINK_BROKEN"
    # The mirror's stored hash does not match a recomputation.  Only the mirror
    # is recomputable (it carries detail); the bucket log omits detail.
    MIRROR_HASH_INVALID = "MIRROR_HASH_INVALID"


@dataclass(slots=True)
class Divergence:
    kind: DivergenceKind
    seq: int
    detail: str = ""


@dataclass(slots=True)
class ChainVerification:
    bucket_count: int
    mirror_count: int
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff the bucket and mirror agree over the window."""
        return not self.divergences


# ---------------------------------------------------------------------------
# Parsing — pure, unit-testable
# ---------------------------------------------------------------------------
def _extract_event_dict(obj: Any) -> Mapping[str, Any] | None:
    """Drill into a Cloud Logging LogEntry to find the audit event payload.

    The sink writes one LogEntry per JSON line.  When the app logs via stdout +
    ``JsonFormatter`` the event JSON is nested inside ``jsonPayload.message``
    (a JSON string); when logged structurally it sits directly in
    ``jsonPayload``.  Either way the event dict carries ``seq`` and ``hash``.
    """
    cur: Any = obj
    for _ in range(5):
        if not isinstance(cur, Mapping):
            return None
        if "seq" in cur and "hash" in cur:
            return cur
        if isinstance(cur.get("jsonPayload"), Mapping):
            cur = cur["jsonPayload"]
            continue
        message = cur.get("message")
        if isinstance(message, str):
            try:
                cur = json.loads(message)
            except json.JSONDecodeError:
                return None
            continue
        return None
    return None


def _event_from_dict(d: Mapping[str, Any]) -> AuditEvent | None:
    """Build an :class:`AuditEvent` from a parsed payload, trusting the stored hash."""
    try:
        raw_detail = d.get("detail", {})
        detail: dict[str, Any] = dict(raw_detail) if isinstance(raw_detail, Mapping) else {}
        return AuditEvent(
            seq=int(d["seq"]),
            prev_hash=str(d["prev_hash"]),
            event_type=str(d["event_type"]),
            actor=str(d["actor"]),
            second_factor=bool(d["second_factor"]),
            timestamp=int(d["timestamp"]),
            detail=detail,
            patient_key=str(d.get("patient_key", "")),
            hash=str(d.get("hash", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_bucket_record(line: str) -> AuditEvent | None:
    """Parse one newline-delimited LogEntry from the audit bucket into an event.

    Returns ``None`` for blank lines, non-JSON, or entries that carry no
    auditable event (e.g. an ``AUDIT_CHAIN_BROKEN`` finding has no ``seq``).
    """
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    payload = _extract_event_dict(obj)
    if payload is None:
        return None
    return _event_from_dict(payload)


def parse_mirror_record(doc: Mapping[str, Any]) -> AuditEvent | None:
    """Build an event from a Firestore ``audit_mirror`` document."""
    return _event_from_dict(doc)


# ---------------------------------------------------------------------------
# Comparison — pure, unit-testable
# ---------------------------------------------------------------------------
def compare_chains(
    bucket: Sequence[AuditEvent],
    mirror: Sequence[AuditEvent],
) -> ChainVerification:
    """Compare two stores of audit events over the same window.

    No I/O — unit-testable with fabricated chains.  Duplicate seqs within a
    store collapse to the last seen, which is itself a malformed-chain symptom
    the continuity checks surface.
    """
    bucket_by_seq: dict[int, AuditEvent] = {e.seq: e for e in bucket}
    mirror_by_seq: dict[int, AuditEvent] = {e.seq: e for e in mirror}
    divergences: list[Divergence] = []

    # Mirror self-consistency: the mirror carries `detail`, so each stored hash
    # is recomputable and each link is checkable.  The first event in the window
    # links to an event outside it, so its link is trusted, not checked.
    mirror_sorted = sorted(mirror_by_seq.values(), key=lambda e: e.seq)
    for idx, ev in enumerate(mirror_sorted):
        if ev.hash and ev.compute_hash() != ev.hash:
            divergences.append(
                Divergence(
                    DivergenceKind.MIRROR_HASH_INVALID,
                    ev.seq,
                    "stored hash does not match recomputation",
                )
            )
        if idx > 0 and ev.prev_hash != mirror_sorted[idx - 1].hash:
            divergences.append(
                Divergence(
                    DivergenceKind.CHAIN_LINK_BROKEN,
                    ev.seq,
                    "mirror prev_hash != previous hash",
                )
            )

    # Bucket linkage: the bucket log omits `detail`, so the hash is NOT
    # recomputable here — only the prev_hash link is checked via stored hashes.
    bucket_sorted = sorted(bucket_by_seq.values(), key=lambda e: e.seq)
    for idx, ev in enumerate(bucket_sorted):
        if idx > 0 and ev.prev_hash != bucket_sorted[idx - 1].hash:
            divergences.append(
                Divergence(
                    DivergenceKind.CHAIN_LINK_BROKEN,
                    ev.seq,
                    "bucket prev_hash != previous hash",
                )
            )

    # Cross-store agreement — the check that makes the boundary meaningful.
    for seq, bev in bucket_by_seq.items():
        mev = mirror_by_seq.get(seq)
        if mev is None:
            divergences.append(
                Divergence(
                    DivergenceKind.BUCKET_MISSING_FROM_MIRROR,
                    seq,
                    "bucket record has no mirror counterpart (tamper case)",
                )
            )
        elif bev.hash != mev.hash:
            divergences.append(
                Divergence(
                    DivergenceKind.HASH_MISMATCH,
                    seq,
                    f"bucket hash {bev.hash[:8]}.. != mirror hash {mev.hash[:8]}..",
                )
            )
    for seq in mirror_by_seq:
        if seq not in bucket_by_seq:
            divergences.append(
                Divergence(
                    DivergenceKind.MIRROR_MISSING_FROM_BUCKET,
                    seq,
                    "mirror record has no bucket counterpart (delivery gap)",
                )
            )

    return ChainVerification(
        bucket_count=len(bucket_by_seq),
        mirror_count=len(mirror_by_seq),
        divergences=divergences,
    )


# ---------------------------------------------------------------------------
# Windowing helpers — pure, unit-testable
# ---------------------------------------------------------------------------
def _cutoff(days: int, *, now: int | None = None) -> int:
    base = now if now is not None else int(time.time())
    return base - days * _SECONDS_PER_DAY


def date_prefixes(days: int, *, now: int | None = None) -> list[str]:
    """Cloud Logging GCS sink object-name prefixes (YYYY/MM/DD) for the window.

    The sink writes objects as ``<YYYY>/<MM>/<DD>/<HH>/<shard>.json``; listing
    with a day prefix avoids scanning the full 2,190-day bucket history.
    """
    base = datetime.fromtimestamp(now, tz=UTC) if now is not None else datetime.now(UTC)
    prefixes = {(base - timedelta(days=i)).strftime("%Y/%m/%d") for i in range(days)}
    return sorted(prefixes)


# ---------------------------------------------------------------------------
# Store readers — production I/O, loaded via importlib (see module docstring)
# ---------------------------------------------------------------------------
def _storage_client() -> Any:
    return importlib.import_module("google.cloud.storage").Client()


def _firestore_module() -> Any:
    return importlib.import_module("google.cloud.firestore")


def read_bucket_events(
    bucket_name: str,
    days: int,
    *,
    now: int | None = None,
) -> list[AuditEvent]:
    """Read and parse audit events from the bucket-locked system of record."""
    cutoff = _cutoff(days, now=now)
    client = _storage_client()
    events: list[AuditEvent] = []
    for prefix in date_prefixes(days, now=now):
        blobs = client.list_blobs(bucket_name, prefix=prefix)
        for blob in blobs:
            text: str = blob.download_as_text()
            for line in text.splitlines():
                ev = parse_bucket_record(line)
                if ev is not None and ev.timestamp >= cutoff:
                    events.append(ev)
    return events


async def read_mirror_events(
    database: str,
    days: int,
    *,
    now: int | None = None,
) -> list[AuditEvent]:
    """Read audit events from the Firestore audit_mirror within the window."""
    cutoff = _cutoff(days, now=now)
    fs = _firestore_module()
    client = fs.AsyncClient(database=database)
    coll = client.collection(_AUDIT_COLLECTION)
    # Single-field range filter on `timestamp` uses the automatic single-field
    # index (audit_mirror has no index exemption).  Ordering is done in Python
    # to avoid needing a (timestamp, seq) composite index that does not exist.
    docs = coll.where(filter=fs.FieldFilter("timestamp", ">=", cutoff)).stream()
    events: list[AuditEvent] = []
    async for doc in docs:
        data: Mapping[str, Any] = doc.to_dict() or {}
        ev = parse_mirror_record(data)
        if ev is not None:
            events.append(ev)
    return events


# ---------------------------------------------------------------------------
# Emission + CLI
# ---------------------------------------------------------------------------
def _emit_stdout(payload: Mapping[str, Any]) -> None:
    """Write one JSON line to stdout — Cloud Run captures it as jsonPayload."""
    sys.stdout.write(json.dumps(payload, default=str, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _emit_finding(verification: ChainVerification, *, reason: str | None = None) -> None:
    payload: dict[str, Any] = {
        "event_type": "AUDIT_CHAIN_BROKEN",
        "error": {"code": ErrorCode.AUDIT_CHAIN_BROKEN.value},
        "bucket_count": verification.bucket_count,
        "mirror_count": verification.mirror_count,
        "divergence_count": len(verification.divergences),
        "divergences": [
            {"kind": d.kind.value, "seq": d.seq, "detail": d.detail}
            for d in verification.divergences
        ],
    }
    if reason is not None:
        payload["reason"] = reason
    _emit_stdout(payload)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_audit_chain",
        description="Cross-store audit-chain verifier (§5.1).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Verify events from the last N days (default: 7).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.config import settings

    args = _parse_args(argv)
    if args.days < 1:
        _emit_stdout(
            {
                "event_type": "AUDIT_CHAIN_BROKEN",
                "error": {"code": ErrorCode.AUDIT_CHAIN_BROKEN.value},
                "reason": f"--days must be >= 1, got {args.days}",
            }
        )
        return 1

    try:
        bucket = read_bucket_events(settings.audit_bucket_name, args.days)
        mirror = asyncio.run(read_mirror_events(settings.firestore_database, args.days))
    except Exception as exc:
        # Cannot read a store ⇒ cannot verify ⇒ treat as broken (fail-closed).
        # A read failure is itself a finding: the immutable trail is unreachable.
        _emit_finding(
            ChainVerification(bucket_count=0, mirror_count=0),
            reason=f"store read failed: {exc}",
        )
        return 1

    verification = compare_chains(bucket, mirror)
    if verification.ok:
        _emit_stdout(
            {
                "event_type": "AUDIT_CHAIN_OK",
                "bucket_count": verification.bucket_count,
                "mirror_count": verification.mirror_count,
                "days": args.days,
            }
        )
        return 0
    _emit_finding(verification)
    return 1


if __name__ == "__main__":
    sys.exit(main())
