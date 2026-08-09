"""Ingest job repository — durable job records, checkpoints, owner-token lease.

A job is a document at ``ingest_jobs/{jobId}`` carrying ``status``,
``lastCheckpointIndex`` and an ``ownerToken`` lease.  The lease is a separate
document at ``ingest_locks/{studyInstanceUidHash}``; :meth:`acquire_lease` is
atomic for a fresh lock and reclaimable once the previous lease has expired,
which is what makes a Cloud Run instance dying mid-ingest recoverable rather than
permanently wedged.
"""

from __future__ import annotations

import hashlib
import time

from app.models.ingest import IngestJob, IngestJobStatus
from app.repositories.base import DocumentStore

JOBS_COLLECTION = "ingest_jobs"
LOCKS_COLLECTION = "ingest_locks"


def study_uid_hash(study_instance_uid: str) -> str:
    """Stable, opaque lock key — the UID itself is never stored in the lock."""
    return hashlib.sha256(study_instance_uid.encode()).hexdigest()


class IngestJobRepository:
    """Persist job records and mediate the per-study ingest lease."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    # -- jobs ----------------------------------------------------------------
    async def create(self, job: IngestJob) -> None:
        await self._store.set(JOBS_COLLECTION, job.job_id, job.model_dump())

    async def get(self, job_id: str) -> IngestJob | None:
        doc = await self._store.get(JOBS_COLLECTION, job_id)
        return IngestJob.model_validate(doc) if doc is not None else None

    async def update(self, job_id: str, data: dict[str, object]) -> None:
        await self._store.update(JOBS_COLLECTION, job_id, data)

    async def find_by_upload(self, upload_id: str) -> IngestJob | None:
        rows = await self._store.query(
            JOBS_COLLECTION, where=[("upload_id", "==", upload_id)], limit=1
        )
        return IngestJob.model_validate(rows[0][1]) if rows else None

    async def list_by_status(
        self, status: IngestJobStatus | None = None, limit: int = 100
    ) -> list[IngestJob]:
        if status is None:
            rows = await self._store.query(JOBS_COLLECTION, limit=limit)
        else:
            rows = await self._store.query(
                JOBS_COLLECTION, where=[("status", "==", status.value)], limit=limit
            )
        return [IngestJob.model_validate(doc) for _id, doc in rows]

    # -- lease ---------------------------------------------------------------
    async def acquire_lease(
        self, study_instance_uid: str, owner_token: str, ttl_seconds: int
    ) -> bool:
        """Atomically acquire the ingest lease for a study.

        Returns ``True`` if the lease was acquired — fresh, reclaimed after the
        previous lease expired, or refreshed by the same owner resuming a
        checkpointed job — and ``False`` if a non-expired lease is held by a
        *different* owner, which the caller surfaces as ``409 INGEST_IN_PROGRESS``.
        """
        lock_id = study_uid_hash(study_instance_uid)
        now = time.time()
        expire_at = now + ttl_seconds
        payload: dict[str, object] = {
            "owner_token": owner_token,
            "expire_at": expire_at,
            "acquired_at": now,
        }
        existing = await self._store.get(LOCKS_COLLECTION, lock_id)
        if existing is None:
            return await self._store.create(LOCKS_COLLECTION, lock_id, payload)
        if float(existing.get("expire_at", 0)) < now:
            # Expired — reclaim by overwriting.
            await self._store.set(LOCKS_COLLECTION, lock_id, payload)
            return True
        if existing.get("owner_token") == owner_token:
            # Same owner resuming a checkpointed job — refresh the lease.
            await self._store.set(LOCKS_COLLECTION, lock_id, payload)
            return True
        return False

    async def release_lease(self, study_instance_uid: str) -> None:
        await self._store.delete(LOCKS_COLLECTION, study_uid_hash(study_instance_uid))
