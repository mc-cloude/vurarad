"""MinIO-backed ``ObjectStore`` — boto3 S3-compatible client.

Used in the on-prem tier where no managed store exists.  Object lock in
**compliance** mode provides WORM semantics equivalent to GCS bucket lock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.storage.base import NO_STORE_CACHE_HEADERS, ObjectMetadata, ObjectRef

logger = logging.getLogger("vurarad.storage.minio")


class MinioObjectStore:
    """``ObjectStore`` backed by an S3-compatible MinIO endpoint."""

    def __init__(self, client: BaseClient, bucket_name: str) -> None:
        self._client = client
        self._bucket_name = bucket_name

    # -- construction --------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Settings, bucket_name: str | None = None) -> MinioObjectStore:
        """Build a store from application settings."""
        endpoint = settings.minio_endpoint
        if not endpoint:
            raise ValueError("minio_endpoint is required when storage_backend is 'minio'")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name=settings.minio_region or "us-east-1",
            config=boto3.session.Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        bucket = bucket_name or settings.pixel_bucket_name
        return cls(client, bucket)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _is_not_found(err: ClientError) -> bool:
        """Return True for 404 / NoSuchKey / NotFound errors."""
        code = err.response.get("Error", {}).get("Code", "")
        return code in ("404", "NoSuchKey", "NotFound")

    def _put_sync(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None,
    ) -> ObjectRef:
        self._client.put_object(
            Bucket=self._bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=dict(metadata) if metadata else {},
        )
        return ObjectRef(bucket=self._bucket_name, key=key)

    def _get_blob_sync(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket_name, Key=key)
        return cast(bytes, resp["Body"].read())

    def _get_range_sync(self, key: str, start: int, end: int) -> bytes:
        resp = self._client.get_object(
            Bucket=self._bucket_name,
            Key=key,
            Range=f"bytes={start}-{end - 1}",
        )
        return cast(bytes, resp["Body"].read())

    def _delete_sync(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket_name, Key=key)
        except ClientError as err:
            if not self._is_not_found(err):
                raise

    def _exists_sync(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket_name, Key=key)
            return True
        except ClientError:
            return False

    def _list_prefix_sync(self, prefix: str, limit: int) -> list[ObjectRef]:
        resp = self._client.list_objects_v2(Bucket=self._bucket_name, Prefix=prefix, MaxKeys=limit)
        return [
            ObjectRef(bucket=self._bucket_name, key=obj["Key"]) for obj in resp.get("Contents", [])
        ]

    def _copy_sync(self, src_key: str, dst_key: str) -> ObjectRef:
        self._client.copy_object(
            Bucket=self._bucket_name,
            Key=dst_key,
            CopySource={"Bucket": self._bucket_name, "Key": src_key},
        )
        return ObjectRef(bucket=self._bucket_name, key=dst_key)

    def _rewrite_sync(
        self,
        src_key: str,
        dst_key: str,
        cache_control: str | None,
        metadata: Mapping[str, str] | None,
    ) -> ObjectRef:
        # Server-side copy with MetadataDirective=REPLACE so the destination's
        # Cache-Control / custom metadata are set without downloading bytes.
        params: dict[str, Any] = {
            "Bucket": self._bucket_name,
            "Key": dst_key,
            "CopySource": {"Bucket": self._bucket_name, "Key": src_key},
            "MetadataDirective": "REPLACE",
        }
        if metadata is not None:
            params["Metadata"] = dict(metadata)
        if cache_control is not None:
            params["CacheControl"] = cache_control
        self._client.copy_object(**params)
        return ObjectRef(bucket=self._bucket_name, key=dst_key)

    def _object_metadata_sync(self, key: str) -> ObjectMetadata:
        resp = self._client.head_object(Bucket=self._bucket_name, Key=key)
        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket_name, key=key),
            size=resp.get("ContentLength", 0),
            content_type=resp.get("ContentType", ""),
            etag=resp.get("ETag", "").strip('"'),
            updated=resp.get("LastModified", datetime.now(UTC)),
            metadata=dict(resp.get("Metadata", {})),
            cache_control=resp.get("CacheControl") or None,
        )

    def _signed_read_url_sync(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: Mapping[str, str] | None,
    ) -> str:
        params: dict[str, Any] = {"Bucket": self._bucket_name, "Key": key}
        headers = {**(response_headers or {}), **NO_STORE_CACHE_HEADERS}
        if "Cache-Control" in headers:
            params["ResponseCacheControl"] = headers["Cache-Control"]
        if "Content-Disposition" in headers:
            params["ResponseContentDisposition"] = headers["Content-Disposition"]
        if "Content-Type" in headers:
            params["ResponseContentType"] = headers["Content-Type"]
        return cast(
            str,
            self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl_seconds),
        )

    def _signed_upload_url_sync(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int,
    ) -> str:
        return cast(
            str,
            self._client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket_name,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=ttl_seconds,
            ),
        )

    def _create_resumable_upload_sync(
        self,
        key: str,
        content_type: str,
        expected_bytes: int,
    ) -> str:
        # S3 multipart: initiate and return the upload ID encoded in a URL.
        resp = self._client.create_multipart_upload(
            Bucket=self._bucket_name,
            Key=key,
            ContentType=content_type,
        )
        upload_id = resp["UploadId"]
        # Return a presigned URL for the first part; the client exchanges
        # the upload_id for subsequent part URLs.
        return cast(
            str,
            self._client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": self._bucket_name,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": 1,
                },
                ExpiresIn=3600,
            ),
        )

    def _supports_bucket_lock_sync(self) -> bool:
        try:
            resp = self._client.get_object_lock_configuration(Bucket=self._bucket_name)
            config = resp.get("ObjectLockConfiguration", {})
            enabled = config.get("ObjectLockEnabled") == "Enabled"
            default_retention = config.get("Rule", {}).get("DefaultRetention", {})
            mode_is_compliance = default_retention.get("Mode") == "COMPLIANCE"
            return bool(enabled and mode_is_compliance)
        except ClientError:
            return False

    def _set_retention_policy_sync(self, retention_days: int) -> None:
        self._client.put_object_lock_configuration(
            Bucket=self._bucket_name,
            ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {
                    "DefaultRetention": {
                        "Mode": "COMPLIANCE",
                        "Days": retention_days,
                    }
                },
            },
        )

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
