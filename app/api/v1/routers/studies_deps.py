# ruff: noqa: B008
"""Dependency providers for the studies router (WP4).

Composition root for the read side: document store, study/patient repositories,
signed-URL service, audit service, and the :class:`StudyService` built on them.
Tests override via ``app.state``; production lazy-builds from settings.

A custom ``require_phi_capability`` dependency returns ``PHI_ACCESS_FORBIDDEN``
for admin (who structurally holds zero PHI capabilities) rather than
``PERMISSION_DENIED``, per §6.3.3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status

from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability, Role, has_capability, is_phi_capability
from app.core.config import Settings
from app.models.study import ViewerScope
from app.repositories.base import DocumentStore, FirestoreDocumentStore
from app.repositories.patient_repo import PatientRepository
from app.repositories.study_repo import StudyRepository
from app.services.audit_service import AuditMirror, AuditService
from app.services.rendition_service import RenditionService
from app.services.signed_url_service import SignedUrlService
from app.services.study_service import StudyService
from app.storage.base import ObjectStore

# Re-export require_mfa so tests can discover it in the dependency tree.
__all__ = [
    "require_phi_capability",
    "require_patient_identity_access",
    "StudyServiceDep",
    "ViewerScopeDep",
    "require_mfa",
]


# ---------------------------------------------------------------------------
# Capability dependency — PHI_ACCESS_FORBIDDEN for admin
# ---------------------------------------------------------------------------
def require_phi_capability(
    capability: Capability,
) -> Callable[[AuthenticatedUser], Awaitable[AuthenticatedUser]]:
    """FastAPI dependency factory — enforce a PHI capability.

    Identical to ``require_capability`` except that admin gets
    ``PHI_ACCESS_FORBIDDEN`` (structural separation) instead of
    ``PERMISSION_DENIED``.
    """

    async def _require(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if has_capability(user.role, capability):
            return user
        if user.role == Role.ADMIN and is_phi_capability(capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "PHI_ACCESS_FORBIDDEN",
                        "message": "PHI access not permitted for this role",
                    }
                },
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": f"Capability '{capability.value}' required",
                }
            },
        )

    return _require


async def require_patient_identity_access(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Restrict patient-identity access to radiologists only.

    Admin and viewer both get ``PHI_ACCESS_FORBIDDEN`` — the patient-identity
    route is the only surface that releases a patient name, and only the
    assigned radiologist (verified by :class:`StudyAccessPolicy`) may call it.
    """
    if user.role == Role.RADIOLOGIST:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": {
                "code": "PHI_ACCESS_FORBIDDEN",
                "message": "PHI access not permitted for this role",
            }
        },
    )


# ---------------------------------------------------------------------------
# Document store
# ---------------------------------------------------------------------------
async def get_document_store(request: Request) -> DocumentStore:
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        settings: Settings = request.app.state.settings
        store = FirestoreDocumentStore.from_settings(settings)
        request.app.state.document_store = store
    return store


DocumentStoreDep = Annotated[DocumentStore, Depends(get_document_store)]


# ---------------------------------------------------------------------------
# Object store (pixel bucket)
# ---------------------------------------------------------------------------
async def get_pixel_object_store(request: Request) -> ObjectStore:
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        from app.core.deps import get_object_store

        # Re-use the shared provider which lazy-builds from settings.
        store = await get_object_store(request)
    return store


PixelObjectStoreDep = Annotated[ObjectStore, Depends(get_pixel_object_store)]


# ---------------------------------------------------------------------------
# Rendition service — three quality tiers and the derived/ lifecycle (WP15)
# ---------------------------------------------------------------------------
async def get_rendition_service(store: PixelObjectStoreDep) -> RenditionService:
    return RenditionService(store)


RenditionServiceDep = Annotated[RenditionService, Depends(get_rendition_service)]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
async def get_audit_mirror(request: Request) -> AuditMirror:
    mirror = getattr(request.app.state, "audit_mirror", None)
    if mirror is None:
        from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror

        mirror = InMemoryAuditMirror()
        request.app.state.audit_mirror = mirror
    return mirror


AuditMirrorDep = Annotated[AuditMirror, Depends(get_audit_mirror)]


async def get_audit_service(mirror: AuditMirrorDep) -> AuditService:
    return AuditService(mirror)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
async def get_study_repo(doc_store: DocumentStoreDep) -> StudyRepository:
    return StudyRepository(doc_store)


StudyRepoDep = Annotated[StudyRepository, Depends(get_study_repo)]


async def get_patient_repo(doc_store: DocumentStoreDep) -> PatientRepository:
    return PatientRepository(doc_store)


PatientRepoDep = Annotated[PatientRepository, Depends(get_patient_repo)]


# ---------------------------------------------------------------------------
# Signed URL service
# ---------------------------------------------------------------------------
async def get_signed_url_service(
    store: PixelObjectStoreDep,
    settings: SettingsDep,
) -> SignedUrlService:
    return SignedUrlService(
        store,
        ttl_seconds=settings.signed_url_ttl_seconds,
        concurrency=settings.sign_blob_concurrency,
    )


SignedUrlServiceDep = Annotated[SignedUrlService, Depends(get_signed_url_service)]


# ---------------------------------------------------------------------------
# Study service
# ---------------------------------------------------------------------------
async def get_study_service(
    study_repo: StudyRepoDep,
    patient_repo: PatientRepoDep,
    signed_url_service: SignedUrlServiceDep,
    audit_service: AuditServiceDep,
    settings: SettingsDep,
) -> StudyService:
    return StudyService(
        study_repo,
        patient_repo,
        signed_url_service,
        audit_service,
        worklist_cap=settings.worklist_cap,
        chunk_max=settings.signed_url_chunk_max,
    )


StudyServiceDep = Annotated[StudyService, Depends(get_study_service)]


# ---------------------------------------------------------------------------
# Settings (local alias to avoid touching the shared deps module)
# ---------------------------------------------------------------------------
async def get_settings_local(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(get_settings_local)]


# ---------------------------------------------------------------------------
# Viewer scope — defaults to empty (deny-by-default)
# ---------------------------------------------------------------------------
async def get_viewer_scope(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ViewerScope:
    """Return the viewer's data-access scope from ``app.state`` or empty.

    In production this would read ``users/{uid}.viewerScope``; for WP4 the
    scope is injected via ``app.state.viewer_scopes`` (a dict keyed by uid) in
    tests, and defaults to empty — so a freshly created viewer can read nothing.
    """
    scopes = getattr(request.app.state, "viewer_scopes", None)
    if scopes is None:
        return ViewerScope()
    scope = scopes.get(user.uid) if isinstance(scopes, dict) else None
    if isinstance(scope, ViewerScope):
        return scope
    return ViewerScope()


ViewerScopeDep = Annotated[ViewerScope, Depends(get_viewer_scope)]
