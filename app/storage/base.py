"""Object-store abstraction — the ONLY type the rest of the app imports.

Backed by GCS in the cloud tier and MinIO (S3-compatible) on-prem.  The rest
of the application depends on ``ObjectStore`` (a structural ``Protocol``),
never on a backend SDK directly.  A CI grep asserts the GCS SDK is imported
**only** inside ``app/storage/gcs.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Cache-Control applied to every pixel-bearing signed read URL.  ``[B10]``
# ---------------------------------------------------------------------------
NO_STORE_CACHE_HEADERS: Mapping[str, str] = {"Cache-Control": "private, no-store"}


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ObjectRef:
    """A reference to a stored object — bucket + key."""

    bucket: str
    key: str


@dataclass(slots=True)
class ObjectMetadata:
    """Metadata for a stored object."""

    ref: ObjectRef
    size: int
    content_type: str
    etag: str
    updated: datetime
    metadata: dict[str, str] = field(default_factory=dict)
    cache_control: str | None = None


# ---------------------------------------------------------------------------
# Protocol — the single contract every backend must satisfy
# ---------------------------------------------------------------------------
@runtime_checkable
class ObjectStore(Protocol):
    """Backend-agnostic object store.

    All methods are ``async``.  Sync SDK calls are wrapped in
    ``asyncio.to_thread`` by the concrete implementations.
    """

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        """Store ``data`` under ``key``.  Returns the object reference."""
        ...

    async def get_blob(self, key: str) -> bytes:
        """Return the full object bytes for ``key``."""
        ...

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        """Return bytes in the half-open range ``[start, end)``."""
        ...

    async def delete(self, key: str) -> None:
        """Delete ``key``.  A missing object is not an error."""
        ...

    async def exists(self, key: str) -> bool:
        """Return ``True`` if ``key`` exists in the bucket."""
        ...

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        """List objects whose key starts with ``prefix``."""
        ...

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Return a time-limited signed URL for reading ``key``.

        ``response_headers`` are applied as response-header overrides on the
        signed URL (e.g. ``Cache-Control: private, no-store``).
        """
        ...

    async def generate_signed_upload_url(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int,
    ) -> str:
        """Return a time-limited signed URL for a single PUT to ``key``."""
        ...

    async def create_resumable_upload(
        self,
        key: str,
        content_type: str,
        expected_bytes: int,
    ) -> str:
        """Initiate a resumable upload session and return the session URL."""
        ...

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        """Copy ``src_key`` to ``dst_key`` within the same bucket."""
        ...

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        """Server-side copy of ``src_key`` to ``dst_key``.

        No object bytes are transferred through the application.  When
        ``cache_control`` is given it is set as the destination object's
        ``Cache-Control`` metadata (e.g. ``private, no-store`` ``[B10]``); when
        ``metadata`` is given it replaces the destination's custom metadata.
        """
        ...

    async def object_metadata(self, key: str) -> ObjectMetadata:
        """Return metadata for ``key``."""
        ...

    @property
    def supports_bucket_lock(self) -> bool:
        """``True`` when the bucket enforces WORM (retention / object lock)."""
        ...

    async def set_retention_policy(self, retention_days: int) -> None:
        """Configure a bucket-level retention / object-lock policy."""
        ...
