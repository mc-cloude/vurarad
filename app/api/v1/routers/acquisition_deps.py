"""Dependency providers for the acquisition (upload + ingest) routers.

Composition root for WP3: object store, document store, audit mirror, and the
services/repositories built on them.  Tests override via ``app.state``; production
lazy-builds from settings.  These mirror the WP1 ``app.core.deps`` pattern but
live here so the shared deps module is untouched.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.deps import get_object_store
from app.models.audit import AuditEvent
from app.repositories.base import DocumentStore, FirestoreDocumentStore
from app.repositories.ingest_job_repo import IngestJobRepository
from app.repositories.series_repo import SeriesRepository
from app.services.audit_service import AuditMirror, AuditService
from app.services.ingest_service import IngestService
from app.services.upload_service import UploadService
from app.storage.base import ObjectStore


class InMemoryAuditMirror:
    """Default audit mirror used when no mirror is wired on ``app.state``.

    Production wires a Firestore-backed mirror; tests inject a recording fake.
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self._events.append(event)

    async def read_chain(self, limit: int = 1000) -> list[AuditEvent]:
        # Most-recent-first so ``chain[0]`` is the previous event for chaining.
        return list(reversed(self._events))[:limit]


async def get_settings_local(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def get_document_store(request: Request) -> DocumentStore:
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        settings: Settings = request.app.state.settings
        store = FirestoreDocumentStore.from_settings(settings)
        request.app.state.document_store = store
    return store


async def get_audit_mirror(request: Request) -> AuditMirror:
    mirror = getattr(request.app.state, "audit_mirror", None)
    if mirror is None:
        mirror = InMemoryAuditMirror()
        request.app.state.audit_mirror = mirror
    return mirror


DocumentStoreDep = Annotated[DocumentStore, Depends(get_document_store)]
AuditMirrorDep = Annotated[AuditMirror, Depends(get_audit_mirror)]
ObjectStoreAcqDep = Annotated[ObjectStore, Depends(get_object_store)]


async def get_upload_service(
    store: ObjectStoreAcqDep,
    doc_store: DocumentStoreDep,
) -> UploadService:
    return UploadService(store, doc_store)


UploadServiceDep = Annotated[UploadService, Depends(get_upload_service)]


async def get_series_repo(doc_store: DocumentStoreDep) -> SeriesRepository:
    return SeriesRepository(doc_store)


SeriesRepoDep = Annotated[SeriesRepository, Depends(get_series_repo)]


async def get_job_repo(doc_store: DocumentStoreDep) -> IngestJobRepository:
    return IngestJobRepository(doc_store)


JobRepoDep = Annotated[IngestJobRepository, Depends(get_job_repo)]


async def get_audit_service(mirror: AuditMirrorDep) -> AuditService:
    return AuditService(mirror)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]


async def get_ingest_service(
    upload_service: UploadServiceDep,
    store: ObjectStoreAcqDep,
    series_repo: SeriesRepoDep,
    job_repo: JobRepoDep,
    audit_service: AuditServiceDep,
) -> IngestService:
    return IngestService(upload_service, store, series_repo, job_repo, audit_service)


IngestServiceDep = Annotated[IngestService, Depends(get_ingest_service)]
