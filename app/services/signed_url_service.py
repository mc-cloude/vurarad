"""Signed URL service — chunked V4 issuance with bounded signBlob concurrency.

Each access-URL chunk issues N signed URLs via ``ObjectStore.generate_signed_read_url``,
which internally calls IAM ``signBlob``.  The calls are network-bound fan-out,
not compute, so they are issued concurrently with ``asyncio.gather`` over a
semaphore of ``sign_blob_concurrency`` (default 32) in-flight calls.

Every signed URL carries ``Cache-Control: private, no-store`` as a response-header
override (§3.6 / B10).  The response object itself also carries the header so a
test can assert it on the response, not in a comment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.models.series import Instance
from app.models.study import AccessUrlChunk, AccessUrlEntry
from app.storage.base import NO_STORE_CACHE_HEADERS, ObjectStore


class SignedUrlService:
    """Issue chunked V4 signed URLs with semaphore-bounded concurrency."""

    def __init__(
        self, store: ObjectStore, *, ttl_seconds: int = 900, concurrency: int = 32
    ) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._semaphore = asyncio.Semaphore(concurrency)

    async def issue_chunk(
        self,
        *,
        study_id: str,
        series_uid: str,
        instances: list[Instance],
        from_stack_index: int,
        count: int,
        series_instance_count: int,
    ) -> AccessUrlChunk:
        """Issue signed URLs for ``instances[from : from + count]``.

        ``instances`` must be ordered by ``stack_index``.  Returns an
        :class:`AccessUrlChunk` with ``nextFromStackIndex`` set to ``None``
        when the end of the series is reached.
        """
        end = min(from_stack_index + count, len(instances))
        chunk_instances = instances[from_stack_index:end]
        actual_count = len(chunk_instances)

        urls = await asyncio.gather(*(self._sign_one(inst) for inst in chunk_instances))

        entries: list[AccessUrlEntry] = []
        for inst, url in zip(chunk_instances, urls, strict=True):
            entries.append(
                AccessUrlEntry(
                    sop_instance_uid=inst.sop_instance_uid,
                    stack_index=inst.stack_index,
                    instance_number=inst.instance_number,
                    size_bytes=inst.size_bytes,
                    number_of_frames=inst.number_of_frames,
                    url=url,
                )
            )

        next_from = end if end < series_instance_count else None
        expires_at = (datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)).isoformat()

        return AccessUrlChunk(
            study_id=study_id,
            series_uid=series_uid,
            expires_at=expires_at,
            from_stack_index=from_stack_index,
            count=actual_count,
            next_from_stack_index=next_from,
            series_instance_count=series_instance_count,
            instances=entries,
        )

    async def _sign_one(self, instance: Instance) -> str:
        """Sign one URL, bounded by the concurrency semaphore."""
        async with self._semaphore:
            return await self._store.generate_signed_read_url(
                instance.object_path,
                self._ttl_seconds,
                response_headers=NO_STORE_CACHE_HEADERS,
            )
