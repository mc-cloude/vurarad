# ruff: noqa: B008
"""Upload routes — authorise resumable uploads scoped to quarantine (§3.7).

Bytes never pass through Cloud Run: ``POST /uploads`` mints GCS resumable
session URLs under the quarantine prefix and returns them; no file body is
accepted.  ``POST /uploads/{uploadId}/complete`` signals completion and starts
the ingest job.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.api.v1.routers.acquisition_deps import IngestServiceDep, UploadServiceDep
from app.core.auth import AuthenticatedUser, get_current_user, require_capability
from app.core.capabilities import Capability
from app.models.ingest import IngestJob, UploadCreate, UploadSession

router = APIRouter(tags=["uploads"])


def _require_idempotency_key(key: str | None) -> str:
    if not key:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Idempotency-Key header is required",
                }
            },
        )
    return key


@router.post(
    "/uploads",
    status_code=201,
    dependencies=[Depends(require_capability(Capability.STUDY_IMPORT))],
)
async def create_upload(
    body: UploadCreate,
    upload_service: UploadServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> UploadSession:
    key = _require_idempotency_key(idempotency_key)
    return await upload_service.create_upload(
        body, tenant=user.tenant_id, actor=user.uid, idempotency_key=key
    )


@router.post(
    "/uploads/{upload_id}/complete",
    status_code=202,
    dependencies=[Depends(require_capability(Capability.STUDY_IMPORT))],
)
async def complete_upload(
    upload_id: str,
    ingest_service: IngestServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> IngestJob:
    key = _require_idempotency_key(idempotency_key)
    return await ingest_service.complete_upload(
        upload_id,
        idempotency_key=key,
        actor=user.uid,
        second_factor=user.is_mfa_verified,
    )
