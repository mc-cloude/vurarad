"""Unit tests for the upload service — resumable session URL minting (§3.7).

Session URLs are minted 100 at a time, scoped under ``quarantine/{tenant}/{uploadId}/``,
and a replay with the same ``(tenant, idempotency_key)`` returns the original
session rather than minting a second one.
"""

from __future__ import annotations

from app.models.ingest import UploadCreate
from app.repositories.base import InMemoryDocumentStore
from app.services.upload_service import MAX_SESSION_URLS_PER_BATCH, UploadService


class RecordingObjectStore:
    """Minimal ObjectStore recording ``create_resumable_upload`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def create_resumable_upload(
        self, key: str, content_type: str, expected_bytes: int
    ) -> str:
        self.calls.append((key, content_type, expected_bytes))
        return f"https://fake.resumable/{key}"


def _request(count: int = 3, total_bytes: int = 4096) -> UploadCreate:
    return UploadCreate(
        source_label="CT-SCANNER-2",
        expected_object_count=count,
        expected_total_bytes=total_bytes,
    )


async def test_mints_at_most_100_urls_per_batch() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    session = await svc.create_upload(
        _request(count=412, total_bytes=1_000_000),
        tenant="tenant-a",
        actor="u1",
        idempotency_key="key-1",
    )
    assert len(session.resumable_session_urls) == MAX_SESSION_URLS_PER_BATCH
    assert len(store.calls) == MAX_SESSION_URLS_PER_BATCH
    assert session.next_object_index == MAX_SESSION_URLS_PER_BATCH
    # expected_bytes=0 → unknown per-object size; client declares length per PUT.
    assert all(b == 0 for _k, _ct, b in store.calls)
    assert all(ct == "application/dicom" for _k, ct, _b in store.calls)


async def test_smaller_count_mints_exact_number() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    session = await svc.create_upload(
        _request(count=3),
        tenant="t",
        actor="u1",
        idempotency_key="k",
    )
    assert len(session.resumable_session_urls) == 3
    assert len(store.calls) == 3


async def test_quarantine_prefix_is_tenant_scoped() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    session = await svc.create_upload(
        _request(count=3),
        tenant="tenant-a",
        actor="u1",
        idempotency_key="k",
    )
    assert session.quarantine_prefix.startswith("quarantine/tenant-a/")
    assert session.quarantine_prefix.startswith(f"quarantine/tenant-a/{session.upload_id}/")
    for url in session.resumable_session_urls:
        assert url.object_name.startswith(session.quarantine_prefix)


async def test_object_names_are_zero_padded() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    session = await svc.create_upload(
        _request(count=3),
        tenant="t",
        actor="u1",
        idempotency_key="k",
    )
    names = [u.object_name.rsplit("/", 1)[-1] for u in session.resumable_session_urls]
    assert names == ["0001.dcm", "0002.dcm", "0003.dcm"]


async def test_idempotent_replay_returns_same_session() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    req = _request(count=3)
    s1 = await svc.create_upload(req, tenant="t", actor="u1", idempotency_key="dup-key")
    first_calls = len(store.calls)
    s2 = await svc.create_upload(req, tenant="t", actor="u1", idempotency_key="dup-key")
    assert s2.upload_id == s1.upload_id
    assert s2.resumable_session_urls == s1.resumable_session_urls
    # No new URLs minted on replay.
    assert len(store.calls) == first_calls


async def test_different_keys_mint_separate_sessions() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    req = _request(count=2)
    s1 = await svc.create_upload(req, tenant="t", actor="u1", idempotency_key="k1")
    s2 = await svc.create_upload(req, tenant="t", actor="u1", idempotency_key="k2")
    assert s1.upload_id != s2.upload_id
    assert len(store.calls) == 4  # 2 + 2


async def test_session_persisted_and_retrievable() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    session = await svc.create_upload(
        _request(count=2),
        tenant="t",
        actor="u1",
        idempotency_key="k",
    )
    retrieved = await svc.get_upload(session.upload_id)
    assert retrieved is not None
    assert retrieved.upload_id == session.upload_id
    assert retrieved.idempotency_key == "k"
    assert retrieved.expected_object_count == 2


async def test_find_by_idempotency_key_across_tenants() -> None:
    store = RecordingObjectStore()
    svc = UploadService(store, InMemoryDocumentStore())
    req = _request(count=2)
    s_a = await svc.create_upload(req, tenant="tenant-a", actor="u1", idempotency_key="shared")
    s_b = await svc.create_upload(req, tenant="tenant-b", actor="u1", idempotency_key="shared")
    # Same key, different tenants → separate sessions.
    assert s_a.upload_id != s_b.upload_id
