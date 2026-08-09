"""Upload service — resumable session URL minting scoped to quarantine.

Mints GCS resumable upload session URLs 100 at a time, all under the quarantine
prefix.  No file body ever passes through Cloud Run: the client PUTs each object
directly to GCS via the session URL, and the quarantine prefix is never
signed-URL-reachable, so an unvalidated object is never viewer-reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ulid import ULID

from app.models.ingest import (
    ResumableSessionUrl,
    UploadCreate,
    UploadSession,
    UploadStatus,
)
from app.repositories.base import DocumentStore
from app.storage.base import ObjectStore

# Session URLs are minted 100 at a time (same chunking policy as §3.6).
MAX_SESSION_URLS_PER_BATCH = 100
UPLOADS_COLLECTION = "uploads"
SESSION_TTL_SECONDS = 86_400  # 24 h — outlives the quarantine lifecycle margin


class UploadService:
    """Mint and persist resumable upload sessions."""

    def __init__(
        self,
        store: ObjectStore,
        doc_store: DocumentStore,
        *,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._doc_store = doc_store
        self._session_ttl = session_ttl_seconds

    async def create_upload(
        self,
        request: UploadCreate,
        *,
        tenant: str,
        actor: str,
        idempotency_key: str,
    ) -> UploadSession:
        """Mint the first batch of resumable session URLs and persist the session.

        Idempotent: a replay with the same ``(tenant, idempotency_key)`` returns
        the original session rather than minting a second one.
        """
        existing = await self.find_by_idempotency_key(tenant, idempotency_key)
        if existing is not None:
            return existing
        upload_id = f"up_{ULID()}"
        prefix = f"quarantine/{tenant}/{upload_id}/"
        batch_size = max(1, min(request.expected_object_count, MAX_SESSION_URLS_PER_BATCH))

        urls: list[ResumableSessionUrl] = []
        for i in range(batch_size):
            object_name = f"{prefix}{i + 1:04d}.dcm"
            # expected_bytes=0 → unknown per-object size; the client declares the
            # length in each resumable PUT.
            session_url = await self._store.create_resumable_upload(
                object_name, "application/dicom", 0
            )
            urls.append(ResumableSessionUrl(object_name=object_name, session_url=session_url))

        now = datetime.now(UTC)
        session = UploadSession(
            upload_id=upload_id,
            tenant=tenant,
            source_label=request.source_label,
            expected_object_count=request.expected_object_count,
            expected_total_bytes=request.expected_total_bytes,
            quarantine_prefix=prefix,
            resumable_session_urls=urls,
            next_object_index=batch_size,
            status=UploadStatus.ACTIVE,
            idempotency_key=idempotency_key,
            actor=actor,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self._session_ttl)).isoformat(),
        )
        await self._doc_store.set(UPLOADS_COLLECTION, upload_id, session.model_dump())
        return session

    async def get_upload(self, upload_id: str) -> UploadSession | None:
        doc = await self._doc_store.get(UPLOADS_COLLECTION, upload_id)
        return UploadSession.model_validate(doc) if doc is not None else None

    async def find_by_idempotency_key(
        self, tenant: str, idempotency_key: str
    ) -> UploadSession | None:
        """Return an existing session for ``(tenant, key)`` — idempotent replay."""
        rows = await self._doc_store.query(
            UPLOADS_COLLECTION,
            where=[
                ("idempotency_key", "==", idempotency_key),
                ("tenant", "==", tenant),
            ],
            limit=1,
        )
        return UploadSession.model_validate(rows[0][1]) if rows else None

    async def mark_completed(self, upload_id: str) -> None:
        session = await self.get_upload(upload_id)
        if session is None:
            return
        session.status = UploadStatus.COMPLETED
        await self._doc_store.set(UPLOADS_COLLECTION, upload_id, session.model_dump())
