"""Dependency injection providers.

These are the composition root for every route — settings, clients, services.
The app factory wires the real implementations; tests override via
app.state or dependency_overrides.
"""

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config import Settings
from app.storage.base import ObjectStore


async def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_object_store(request: Request) -> ObjectStore:
    """Lazy-build and cache the pixel-object store on ``app.state``."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        from app.storage.factory import build_object_store

        settings: Settings = request.app.state.settings
        store = build_object_store(settings)
        request.app.state.object_store = store
    return store


ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store)]


async def get_audit_object_store(request: Request) -> ObjectStore:
    """Lazy-build and cache the audit-object store on ``app.state``.

    Uses the audit bucket, not the pixel bucket.
    """
    store = getattr(request.app.state, "audit_object_store", None)
    if store is None:
        from app.storage.factory import build_object_store

        settings: Settings = request.app.state.settings
        store = build_object_store(settings, bucket_name=settings.audit_bucket_name)
        request.app.state.audit_object_store = store
    return store


AuditObjectStoreDep = Annotated[ObjectStore, Depends(get_audit_object_store)]
