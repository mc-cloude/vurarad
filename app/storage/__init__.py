"""Object-store abstraction — public exports.

The rest of the application imports ``ObjectStore`` from here, never a
backend SDK directly.
"""

from __future__ import annotations

from app.storage.base import NO_STORE_CACHE_HEADERS, ObjectMetadata, ObjectRef, ObjectStore
from app.storage.factory import build_object_store

__all__ = [
    "NO_STORE_CACHE_HEADERS",
    "ObjectMetadata",
    "ObjectRef",
    "ObjectStore",
    "build_object_store",
]
