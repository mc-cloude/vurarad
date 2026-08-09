"""SignedUrlService — chunk bounds, TTL, header assertions, semaphore ceiling.

Tests that:
- A chunk of N instances returns exactly N entries with correct stack indices.
- ``nextFromStackIndex`` is set correctly (null at end).
- ``expiresAt`` is ~now + TTL.
- Each signed-URL call carries ``Cache-Control: private, no-store``.
- The semaphore ceiling (32) is actually concurrent: 250 URLs against a fake
  signer with 20 ms delay complete in < 1 500 ms, proving the calls are not
  serialized.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime

from app.models.series import Instance
from app.services.signed_url_service import SignedUrlService
from app.storage.base import ObjectMetadata, ObjectRef


# ---------------------------------------------------------------------------
# Fake signBlob store — configurable delay, records headers
# ---------------------------------------------------------------------------
class DelayedSignStore:
    """In-memory ObjectStore whose signed-URL call sleeps ``delay_ms``."""

    def __init__(self, delay_ms: int = 20) -> None:
        self.delay_ms = delay_ms
        self.header_records: list[Mapping[str, str] | None] = []
        self.call_count = 0
        self._max_concurrent = 0
        self._current_concurrent = 0

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: Mapping[str, str] | None = None,
    ) -> str:
        self.call_count += 1
        self.header_records.append(response_headers)
        self._current_concurrent += 1
        self._max_concurrent = max(self._max_concurrent, self._current_concurrent)
        await asyncio.sleep(self.delay_ms / 1000)
        self._current_concurrent -= 1
        return f"https://signed.example/{key}?ttl={ttl_seconds}"

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    # -- unused ObjectStore methods (stubs) ---------------------------------
    async def put(
        self, key: str, data: bytes, content_type: str, metadata: Mapping[str, str] | None = None
    ) -> ObjectRef:
        return ObjectRef(bucket="b", key=key)

    async def get_blob(self, key: str) -> bytes:
        return b""

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return b""

    async def delete(self, key: str) -> None:
        pass

    async def exists(self, key: str) -> bool:
        return False

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return []

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        return ObjectRef(bucket="b", key=dst_key)

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        return ObjectRef(bucket="b", key=dst_key)

    async def object_metadata(self, key: str) -> ObjectMetadata:
        return ObjectMetadata(
            ref=ObjectRef(bucket="b", key=key),
            size=0,
            content_type="",
            etag="",
            updated=datetime.now(UTC),
        )

    async def generate_signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int
    ) -> str:
        return f"https://upload.example/{key}"

    async def create_resumable_upload(
        self, key: str, content_type: str, expected_bytes: int
    ) -> str:
        return f"https://resumable.example/{key}"

    @property
    def supports_bucket_lock(self) -> bool:
        return True

    async def set_retention_policy(self, retention_days: int) -> None:
        pass


# ---------------------------------------------------------------------------
# Instance factory
# ---------------------------------------------------------------------------
def _instances(n: int, *, prefix: str = "1.2.3.") -> list[Instance]:
    return [
        Instance(
            sop_instance_uid=f"{prefix}{i}",
            stack_index=i,
            instance_number=i + 1,
            number_of_frames=1,
            size_bytes=526336,
            object_path=f"studies/st_test/se_test/{i:04d}.dcm",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_chunk_returns_correct_entries_and_stack_indices() -> None:
    """A chunk of 5 instances from index 10 returns 5 entries, indices 10-14."""
    store = DelayedSignStore(delay_ms=0)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(20)

    chunk = asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=10,
            count=5,
            series_instance_count=20,
        )
    )

    assert chunk.from_stack_index == 10
    assert chunk.count == 5
    assert chunk.series_instance_count == 20
    assert chunk.next_from_stack_index == 15
    assert len(chunk.instances) == 5
    assert [e.stack_index for e in chunk.instances] == [10, 11, 12, 13, 14]
    assert all(e.url.startswith("https://signed.example/") for e in chunk.instances)


def test_next_from_stack_index_null_at_end() -> None:
    """When the chunk reaches the end, nextFromStackIndex is None."""
    store = DelayedSignStore(delay_ms=0)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(10)

    chunk = asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=5,
            count=10,
            series_instance_count=10,
        )
    )

    assert chunk.count == 5  # only 5 remaining
    assert chunk.next_from_stack_index is None


def test_partial_chunk_at_boundary() -> None:
    """A chunk that spans the end returns only the remaining instances."""
    store = DelayedSignStore(delay_ms=0)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(824)

    chunk = asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=750,
            count=250,
            series_instance_count=824,
        )
    )

    assert chunk.count == 74  # 824 - 750
    assert chunk.next_from_stack_index is None


def test_expires_at_is_ttl_seconds_in_future() -> None:
    """expiresAt is approximately now + TTL."""
    store = DelayedSignStore(delay_ms=0)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(3)

    before = datetime.now(UTC)
    chunk = asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=0,
            count=3,
            series_instance_count=3,
        )
    )
    after = datetime.now(UTC)

    from datetime import timedelta

    expires = datetime.fromisoformat(chunk.expires_at)
    assert before + timedelta(seconds=899) <= expires <= after + timedelta(seconds=901)


def test_each_url_call_carries_no_store_cache_headers() -> None:
    """Every signed-URL call must carry Cache-Control: private, no-store."""
    store = DelayedSignStore(delay_ms=0)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(5)

    asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=0,
            count=5,
            series_instance_count=5,
        )
    )

    assert len(store.header_records) == 5
    for headers in store.header_records:
        assert headers is not None
        assert headers.get("Cache-Control") == "private, no-store"


def test_250_urls_complete_under_1500ms_with_concurrency() -> None:
    """250 URLs against 20ms fake signer complete in < 1500ms.

    Serialised: 250 × 20ms = 5000ms.  With semaphore 32: ceil(250/32) × 20ms
    ≈ 160ms.  The < 1500ms budget proves the semaphore is actually concurrent.
    """
    store = DelayedSignStore(delay_ms=20)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(250)

    start = time.perf_counter()
    asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=0,
            count=250,
            series_instance_count=250,
        )
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 1500, f"250 URLs took {elapsed_ms:.0f}ms, expected < 1500ms"
    assert store.max_concurrent > 1, "Semaphore did not allow concurrency"
    assert store.call_count == 250


def test_semaphore_ceiling_is_32() -> None:
    """The semaphore allows at most 32 concurrent signBlob calls."""
    store = DelayedSignStore(delay_ms=50)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(250)

    asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=0,
            count=250,
            series_instance_count=250,
        )
    )

    assert store.max_concurrent <= 32
    assert store.max_concurrent >= 30  # close to the ceiling


def test_empty_chunk_returns_no_instances() -> None:
    """A chunk from beyond the end returns 0 instances with null next."""
    store = DelayedSignStore(delay_ms=0)
    svc = SignedUrlService(store, ttl_seconds=900, concurrency=32)
    instances = _instances(5)

    chunk = asyncio.run(
        svc.issue_chunk(
            study_id="st_test",
            series_uid="se_test",
            instances=instances,
            from_stack_index=5,
            count=250,
            series_instance_count=5,
        )
    )

    assert chunk.count == 0
    assert chunk.next_from_stack_index is None
    assert len(chunk.instances) == 0
