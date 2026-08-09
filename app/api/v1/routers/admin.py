# ruff: noqa: B008
"""Admin routes — user management and patient erasure.

- ``GET /admin/users`` (user:manage) — list operators (NO PHI).
- ``POST /admin/users/{uid}/role`` (user:manage) — change role, bump
  claimsVersion + revoke tokens.
- ``POST /admin/users/{uid}/disable`` (user:manage) — enable/disable.
- ``DELETE /admin/patients/{patientKey}`` (compliance:purge) — erase a
  patient; requires a *fresh* second-factor challenge (300s rule).

Every route carries ``get_current_user`` + ``require_mfa`` at the router level,
so a first-factor-only token gets ``403 MFA_REQUIRED`` and a radiologist (who
holds none of ``user:manage`` / ``compliance:purge``) gets ``403`` on every
route.  Erasure additionally requires a freshly-verified TOTP code.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.core.auth import (
    AuthenticatedUser,
    SecondFactorVerifier,
    get_current_user,
    require_capability,
    require_mfa,
)
from app.core.capabilities import Capability
from app.core.deps import get_object_store
from app.models.admin import (
    AdminUser,
    DisableUserRequest,
    ErasureRequest,
    ErasureResponse,
    RoleUpdateRequest,
    UserListResponse,
)
from app.repositories.base import DocumentStore, FirestoreDocumentStore
from app.services.admin_service import AdminService, FirebaseUserDirectory, UserDirectory
from app.services.audit_service import AuditService
from app.services.erasure_service import ErasureService
from app.storage.base import ObjectStore

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------
async def get_user_directory(request: Request) -> UserDirectory:
    directory = getattr(request.app.state, "user_directory", None)
    if directory is None:
        directory = FirebaseUserDirectory()
        request.app.state.user_directory = directory
    return directory


UserDirectoryDep = Annotated[UserDirectory, Depends(get_user_directory)]


async def get_admin_service(directory: UserDirectoryDep) -> AdminService:
    return AdminService(directory)


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]


async def get_document_store(request: Request) -> DocumentStore:
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        settings = request.app.state.settings
        store = FirestoreDocumentStore.from_settings(settings)
        request.app.state.document_store = store
    return store


DocumentStoreDep = Annotated[DocumentStore, Depends(get_document_store)]


PixelStoreDep = Annotated[ObjectStore, Depends(get_object_store)]


async def get_audit_service(request: Request) -> AuditService:
    mirror = getattr(request.app.state, "audit_mirror", None)
    if mirror is None:
        from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror

        mirror = InMemoryAuditMirror()
        request.app.state.audit_mirror = mirror
    return AuditService(mirror)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]


async def get_erasure_service(
    doc_store: DocumentStoreDep,
    pixel_store: PixelStoreDep,
    audit_service: AuditServiceDep,
) -> ErasureService:
    return ErasureService(doc_store, pixel_store, audit_service)


ErasureServiceDep = Annotated[ErasureService, Depends(get_erasure_service)]


async def get_second_factor_verifier(request: Request) -> SecondFactorVerifier | None:
    return getattr(request.app.state, "second_factor_verifier", None)


SecondFactorVerifierDep = Annotated[
    SecondFactorVerifier | None, Depends(get_second_factor_verifier)
]


# ---------------------------------------------------------------------------
# Fresh second-factor — required for erasure (300s rule)
# ---------------------------------------------------------------------------
async def require_fresh_mfa(
    verifier: SecondFactorVerifierDep,
    user: AuthenticatedUser = Depends(get_current_user),
    mfa_code: str | None = Header(default=None, alias="X-MFA-Code"),
) -> AuthenticatedUser:
    """Require a freshly-verified TOTP challenge (the 300s freshness rule).

    Erasure is irreversible, so a stale MFA session is insufficient — the
    operator must re-assert their second factor in this request.
    """
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "MFA_REQUIRED",
                    "message": "Fresh second-factor verification is required",
                }
            },
        )
    if not mfa_code:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "MFA_REQUIRED",
                    "message": "Fresh second-factor code is required for erasure",
                }
            },
        )
    ok = await verifier.verify_totp(user.uid, mfa_code)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "MFA_CHALLENGE_FAILED",
                    "message": "Fresh second-factor verification failed",
                }
            },
        )
    return user


FreshMfaUserDep = Annotated[AuthenticatedUser, Depends(require_fresh_mfa)]


# ---------------------------------------------------------------------------
# GET /admin/users — list operators (NO PHI)
# ---------------------------------------------------------------------------
@router.get(
    "/users",
    dependencies=[Depends(require_capability(Capability.USER_MANAGE))],
    response_model=UserListResponse,
)
async def list_users(
    admin_service: AdminServiceDep,
    page_token: str | None = Query(default=None, alias="pageToken"),
    max_results: int = Query(default=100, ge=1, le=1000, alias="maxResults"),
) -> UserListResponse:
    return await admin_service.list_users(
        page_token=page_token, max_results=max_results
    )


# ---------------------------------------------------------------------------
# POST /admin/users/{uid}/role — change role + revoke tokens
# ---------------------------------------------------------------------------
@router.post(
    "/users/{uid}/role",
    dependencies=[Depends(require_capability(Capability.USER_MANAGE))],
    response_model=AdminUser,
)
async def set_role(
    uid: str,
    body: RoleUpdateRequest,
    admin_service: AdminServiceDep,
) -> AdminUser:
    return await admin_service.set_role(uid, body.role)


# ---------------------------------------------------------------------------
# POST /admin/users/{uid}/disable — enable/disable
# ---------------------------------------------------------------------------
@router.post(
    "/users/{uid}/disable",
    dependencies=[Depends(require_capability(Capability.USER_MANAGE))],
    response_model=AdminUser,
)
async def disable_user(
    uid: str,
    body: DisableUserRequest,
    admin_service: AdminServiceDep,
) -> AdminUser:
    return await admin_service.disable_user(uid, body.disabled)


# ---------------------------------------------------------------------------
# DELETE /admin/patients/{patientKey} — erasure (compliance:purge, fresh 2FA)
# ---------------------------------------------------------------------------
@router.delete(
    "/patients/{patient_key}",
    dependencies=[Depends(require_capability(Capability.COMPLIANCE_PURGE))],
    response_model=ErasureResponse,
)
async def erase_patient(
    patient_key: str,
    body: ErasureRequest,
    erasure_service: ErasureServiceDep,
    user: FreshMfaUserDep,
) -> ErasureResponse:
    return await erasure_service.erase_patient(
        patient_key,
        body.confirm_patient_ref,
        actor=user.uid,
        second_factor=True,
    )
