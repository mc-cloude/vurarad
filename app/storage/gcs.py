"""GCS-backed ``ObjectStore`` — V4 signed URLs via signBlob, no private key."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import google.api_core.exceptions
import google.cloud.storage as storage

from app.core.config import Settings
from app.storage.base import NO_STORE_CACHE_HEADERS, ObjectMetadata, ObjectRef, ObjectStore

logger = logging.getLogger("vurarad.storage.gcs")


class GcsObjectStore:
    """``ObjectStore`` backed by the GCS SDK.

    V4 signed URLs are produced via ``signBlob`` on the runtime service
    account — **no private key JSON is written to disk**.  All synchronous
    SDK calls are wrapped in ``asyncio.to_thread``.
    """

    def __init__(
        self,
        client: storage.Client,
        bucket_name: str,
        *,
        signing_credentials: Any = None,
    ) -> None:
        self._client = client
        self._bucket_name = bucket_name
        self._bucket = client.bucket(bucket_name)
        self._signing_credentials = signing_credentials

    # -- construction --------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Settings, bucket_name: str | None = None) -> GcsObjectStore:
        """Build a store from application settings using default credentials."""
        import google.auth

        credentials, _ = google.auth.default()
        client = storage.Client(
            project=settings.gcp_project_id,
            credentials=credentials,
        )
        bucket = bucket_name or settings.pixel_bucket_name
        return cls(client, bucket)

    # -- helpers -------------------------------------------------------------
    def _blob(self, key: str) -> storage.Blob:
        return self._bucket.blob(key)

    def _put_sync(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None,
    ) -> ObjectRef:
        blob = self._blob(key)
        blob.metadata = dict(metadata) if metadata else None
        blob.upload_from_string(data, content_type=content_type)
        return ObjectRef(bucket=self._bucket_name, key=key)

    def _get_blob_sync(self, key: str) -> bytes:
        blob = self._bucket.blob(key)
        return cast(bytes, blob.download_as_bytes())

    def _get_range_sync(self, key: str, start: int, end: int) -> bytes:
        blob = self._bucket.blob(key)
        return cast(bytes, blob.download_as_bytes(start=start, end=end - 1))

    def _delete_sync(self, key: str) -> None:
        with contextlib.suppress(google.api_core.exceptions.NotFound):
            self._bucket.blob(key).delete()

    def _exists_sync(self, key: str) -> bool:
        return bool(self._bucket.blob(key).exists())

    def _list_prefix_sync(self, prefix: str, limit: int) -> list[ObjectRef]:
        return [
            ObjectRef(bucket=self._bucket_name, key=blob.name)
            for blob in self._bucket.list_blobs(prefix=prefix, max_results=limit)
        ]

    def _copy_sync(self, src_key: str, dst_key: str) -> ObjectRef:
        self._bucket.copy_blob(self._bucket.blob(src_key), self._bucket, dst_key)
        return ObjectRef(bucket=self._bucket_name, key=dst_key)

    def _rewrite_sync(
        self,
        src_key: str,
        dst_key: str,
        cache_control: str | None,
        metadata: Mapping[str, str] | None,
    ) -> ObjectRef:
        # Server-side copy (no bytes through the app), then patch the
        # destination's Cache-Control / custom metadata.
        self._bucket.copy_blob(self._bucket.blob(src_key), self._bucket, dst_key)
        dest = self._bucket.blob(dst_key)
        if cache_control is not None:
            dest.cache_control = cache_control
        if metadata is not None:
            dest.metadata = dict(metadata)
        if cache_control is not None or metadata is not None:
            dest.patch()
        return ObjectRef(bucket=self._bucket_name, key=dst_key)

    def _object_metadata_sync(self, key: str) -> ObjectMetadata:
        blob = self._bucket.blob(key)
        blob.reload()
        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket_name, key=key),
            size=blob.size or 0,
            content_type=blob.content_type or "",
            etag=blob.etag or "",
            updated=blob.updated or datetime.now(UTC),
            metadata=dict(blob.metadata) if blob.metadata else {},
            cache_control=blob.cache_control or None,
        )

    def _signed_read_url_sync(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: Mapping[str, str] | None,
    ) -> str:
        blob = self._bucket.blob(key)
        headers = {**(response_headers or {}), **NO_STORE_CACHE_HEADERS}
        # GCS uses query-parameter overrides for response headers on signed URLs
        query_params: dict[str, str] = {}
        if "Cache-Control" in headers:
            query_params["response-cache-control"] = headers["Cache-Control"]
        if "Content-Disposition" in headers:
            query_params["response-content-disposition"] = headers["Content-Disposition"]
        if "Content-Type" in headers:
            query_params["response-content-type"] = headers["Content-Type"]
        return cast(
            str,
            blob.generate_signed_url(
                expiration=timedelta(seconds=ttl_seconds),
                version="v4",
                method="GET",
                query_parameters=query_params,
                credentials=self._signing_credentials or self._client._credentials,
            ),
        )

    def _signed_upload_url_sync(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int,
    ) -> str:
        blob = self._bucket.blob(key)
        return cast(
            str,
            blob.generate_signed_url(
                expiration=timedelta(seconds=ttl_seconds),
                version="v4",
                method="PUT",
                content_type=content_type,
                credentials=self._signing_credentials or self._client._credentials,
            ),
        )

    def _create_resumable_upload_sync(
        self,
        key: str,
        content_type: str,
        expected_bytes: int,
    ) -> str:
        blob = self._bucket.blob(key)
        # size=None lets the client stream an object whose length is not known
        # up front (the per-object size is not declared at session-mint time).
        size: int | None = expected_bytes if expected_bytes > 0 else None
        return cast(
            str,
            blob.create_resumable_upload_session(content_type=content_type, size=size),
        )

    def _supports_bucket_lock_sync(self) -> bool:
        try:
            self._bucket.reload()
            retention = self._bucket.retention_period
            return retention is not None and retention > 0
        except Exception:  # noqa: BLE001
            return False

    def _set_retention_policy_sync(self, retention_days: int) -> None:
        self._bucket.retention_period = retention_days * 86400
        self._bucket.patch()

    # -- async public API ----------------------------------------------------
    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        return await asyncio.to_thread(self._put_sync, key, data, content_type, metadata)

    async def get_blob(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get_blob_sync, key)

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return await asyncio.to_thread(self._get_range_sync, key, start, end)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, key)

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return await asyncio.to_thread(self._list_prefix_sync, prefix, limit)

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        return await asyncio.to_thread(self._copy_sync, src_key, dst_key)

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        return await asyncio.to_thread(
            self._rewrite_sync, src_key, dst_key, cache_control, metadata
        )

    async def object_metadata(self, key: str) -> ObjectMetadata:
        return await asyncio.to_thread(self._object_metadata_sync, key)

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: Mapping[str, str] | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._signed_read_url_sync, key, ttl_seconds, response_headers
        )

    async def generate_signed_upload_url(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int,
    ) -> str:
        return await asyncio.to_thread(self._signed_upload_url_sync, key, content_type, ttl_seconds)

    async def create_resumable_upload(
        self,
        key: str,
        content_type: str,
        expected_bytes: int,
    ) -> str:
        return await asyncio.to_thread(
            self._create_resumable_upload_sync, key, content_type, expected_bytes
        )

    @property
    def supports_bucket_lock(self) -> bool:
        return self._supports_bucket_lock_sync()

    async def set_retention_policy(self, retention_days: int) -> None:
        await asyncio.to_thread(self._set_retention_policy_sync, retention_days)

    # -- convenience ---------------------------------------------------------
    @property
    def _bucket_name_attr(self) -> str:
        return self._bucket_name


# Structural typing: GcsObjectStore satisfies ObjectStore at runtime.
_object_store_check: ObjectStore = cast(Any, None)  # type: ignore[unused-ignore]
