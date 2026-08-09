# ruff: noqa: B008
"""Audit routes — query and export.

- ``GET /audit`` (audit:read) — filtered, paginated audit read with chain
  verification.  ``from`` + ``to`` are required; a window wider than 92 days
  yields ``422 AUDIT_WINDOW_TOO_WIDE``.  Every read writes an ``AUDIT_VIEWED``
  record capturing the filter parameters.
- ``POST /audit/exports`` (audit:export) — NDJSON export to the
  ``vurarad-audit-exports`` bucket with a 10-minute signed URL and an
  ``AUDIT_EXPORTED`` audit record.

A radiologist (who holds neither ``audit:read`` nor ``audit:export``) gets
``403`` on every route.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.auth import AuthenticatedUser, get_current_user, require_capability, require_mfa
from app.core.capabilities import Capability
from app.core.config import Settings
from app.models.admin import (
    AuditExportRequest,
    AuditExportResponse,
    AuditFilter,
    AuditQueryResponse,
)
from app.repositories.base import DocumentStore, FirestoreDocumentStore
from app.services.audit_export_service import AuditExportService
from app.services.audit_query_service import (
    AuditQueryService,
    AuditReadStore,
    FirestoreAuditReadStore,
)
from app.services.audit_service import AuditService
from app.storage.base import ObjectStore

router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)

# Locked bucket for NDJSON audit exports.
_AUDIT_EXPORTS_BUCKET = "vurarad-audit-exports"


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------
async def get_document_store(request: Request) -> DocumentStore:
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        settings: Settings = request.app.state.settings
        store = FirestoreDocumentStore.from_settings(settings)
        request.app.state.document_store = store
    return store


DocumentStoreDep = Annotated[DocumentStore, Depends(get_document_store)]


async def get_audit_service(request: Request) -> AuditService:
    mirror = getattr(request.app.state, "audit_mirror", None)
    if mirror is None:
        from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror

        mirror = InMemoryAuditMirror()
        request.app.state.audit_mirror = mirror
    return AuditService(mirror)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]


async def get_audit_read_store(request: Request) -> AuditReadStore:
    store = getattr(request.app.state, "audit_read_store", None)
    if store is None:
        doc_store = await get_document_store(request)
        store = FirestoreAuditReadStore(doc_store)
        request.app.state.audit_read_store = store
    return store


AuditReadStoreDep = Annotated[AuditReadStore, Depends(get_audit_read_store)]


async def get_audit_query_service(
    read_store: AuditReadStoreDep,
    audit_service: AuditServiceDep,
) -> AuditQueryService:
    return AuditQueryService(read_store, audit_service)


AuditQueryServiceDep = Annotated[AuditQueryService, Depends(get_audit_query_service)]


async def get_export_object_store(request: Request) -> ObjectStore:
    store = getattr(request.app.state, "export_object_store", None)
    if store is None:
        from app.storage.factory import build_object_store

        settings: Settings = request.app.state.settings
        store = build_object_store(settings, bucket_name=_AUDIT_EXPORTS_BUCKET)
        request.app.state.export_object_store = store
    return store


ExportStoreDep = Annotated[ObjectStore, Depends(get_export_object_store)]


async def get_audit_export_service(
    read_store: AuditReadStoreDep,
    export_store: ExportStoreDep,
    audit_service: AuditServiceDep,
) -> AuditExportService:
    return AuditExportService(read_store, export_store, audit_service)


AuditExportServiceDep = Annotated[AuditExportService, Depends(get_audit_export_service)]


# ---------------------------------------------------------------------------
# GET /audit — filtered, paginated, chain-verified audit read
# ---------------------------------------------------------------------------
@router.get(
    "",
    dependencies=[Depends(require_capability(Capability.AUDIT_READ))],
    response_model=AuditQueryResponse,
)
async def query_audit(
    audit_query_service: AuditQueryServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    from_: int = Query(alias="from"),
    to: int = Query(alias="to"),
    actor: str | None = Query(default=None),
    action: str | None = Query(default=None),
    patient_key: str | None = Query(default=None, alias="patientKey"),
    study_id: str | None = Query(default=None, alias="studyId"),
    limit: int = Query(default=100, ge=1, le=1000),
    page_token: str | None = Query(default=None, alias="pageToken"),
) -> AuditQueryResponse:
    filters = AuditFilter(
        from_=from_,
        to=to,
        actor=actor,
        action=action,
        patient_key=patient_key,
        study_id=study_id,
        limit=limit,
        page_token=page_token,
    )
    return await audit_query_service.query_audit(
        filters, actor=user.uid, second_factor=user.is_mfa_verified
    )


# ---------------------------------------------------------------------------
# POST /audit/exports — NDJSON export with signed URL
# ---------------------------------------------------------------------------
@router.post(
    "/exports",
    dependencies=[Depends(require_capability(Capability.AUDIT_EXPORT))],
    response_model=AuditExportResponse,
)
async def export_audit(
    body: AuditExportRequest,
    audit_export_service: AuditExportServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuditExportResponse:
    filters = AuditFilter(from_=body.from_, to=body.to)
    return await audit_export_service.export_audit(
        filters,
        reason=body.reason,
        recipient=body.recipient,
        actor=user.uid,
        second_factor=user.is_mfa_verified,
    )
