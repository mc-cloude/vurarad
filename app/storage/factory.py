"""Factory — ``build_object_store(settings)`` returns the configured backend.

Backend imports are lazy so that a MinIO-only deployment never imports the
GCS SDK (and vice-versa).
"""

from __future__ import annotations

from app.core.config import Settings
from app.storage.base import ObjectStore


def build_object_store(settings: Settings, bucket_name: str | None = None) -> ObjectStore:
    """Return the configured ``ObjectStore`` for the current settings.

    ``settings.storage_backend`` selects between ``"gcs"`` and ``"minio"``.
    Imports are deferred so an on-prem MinIO deployment does not need the
    GCS SDK installed.
    """
    backend = settings.storage_backend

    if backend == "minio":
        from app.storage.minio import MinioObjectStore

        return MinioObjectStore.from_settings(settings, bucket_name)

    # Default: GCS
    from app.storage.gcs import GcsObjectStore

    return GcsObjectStore.from_settings(settings, bucket_name)
