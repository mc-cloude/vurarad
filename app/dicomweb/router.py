# ruff: noqa: B008
"""DICOMweb router — QIDO-RS, WADO-RS, STOW-RS paths (§3.20.1).

Every path carries ``get_current_user``, ``require_mfa``, a capability, and
(where study-scoped) ``StudyAccessPolicy``.  DICOMweb is NOT an
unauthenticated side door.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.core.auth import AuthenticatedUser, get_current_user, require_capability, require_mfa
from app.core.capabilities import Capability
from app.core.deps import ObjectStoreDep, SettingsDep
from app.dicomweb.deps import MetadataStoreDep, StudyAccessPolicy
from app.dicomweb.qido import query_instances, query_series, query_studies
from app.dicomweb.stow import store_instances, stow_json_response
from app.dicomweb.wado import (
    retrieve_frames,
    retrieve_instance,
    retrieve_instance_metadata,
    retrieve_rendered,
    retrieve_series,
    retrieve_study,
)

router = APIRouter(
    prefix="/dicomweb",
    tags=["dicomweb"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


def _multipart_response(body: bytes, boundary: str, part_type: str) -> Response:
    """Build a ``multipart/related`` Response with no-store cache headers."""
    content_type = f'multipart/related; type="{part_type}"; boundary={boundary}'
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "private, no-store"},
    )


# ---------------------------------------------------------------------------
# QIDO-RS
# ---------------------------------------------------------------------------
@router.get(
    "/studies",
    dependencies=[Depends(require_capability(Capability.STUDY_SEARCH))],
)
async def qido_studies(
    request: Request,
    store: MetadataStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    limit = int(request.query_params.get("limit", "100"))
    offset = request.query_params.get("offset")
    return await query_studies(store, user, limit=limit, offset=offset)


@router.get(
    "/studies/{study_uid}/series",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def qido_series(
    study: StudyAccessPolicy,
    request: Request,
    store: MetadataStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    limit = int(request.query_params.get("limit", "100"))
    offset = request.query_params.get("offset")
    return await query_series(study, store, user, limit=limit, offset=offset)


@router.get(
    "/studies/{study_uid}/series/{series_uid}/instances",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def qido_instances(
    study: StudyAccessPolicy,
    series_uid: str,
    request: Request,
    store: MetadataStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    limit = int(request.query_params.get("limit", "100"))
    offset = request.query_params.get("offset")
    return await query_instances(study, series_uid, store, user, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# WADO-RS — multipart retrieval
# ---------------------------------------------------------------------------
@router.get(
    "/studies/{study_uid}",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def wado_study(
    study: StudyAccessPolicy,
    store: MetadataStoreDep,
    object_store: ObjectStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    body, boundary = await retrieve_study(study, store, object_store, user)
    return _multipart_response(body, boundary, "application/dicom")


@router.get(
    "/studies/{study_uid}/series/{series_uid}",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def wado_series(
    study: StudyAccessPolicy,
    series_uid: str,
    store: MetadataStoreDep,
    object_store: ObjectStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    body, boundary = await retrieve_series(study, series_uid, store, object_store, user)
    return _multipart_response(body, boundary, "application/dicom")


@router.get(
    "/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def wado_instance(
    study: StudyAccessPolicy,
    series_uid: str,
    sop_uid: str,
    store: MetadataStoreDep,
    object_store: ObjectStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    body, boundary = await retrieve_instance(study, series_uid, sop_uid, store, object_store, user)
    return _multipart_response(body, boundary, "application/dicom")


@router.get(
    "/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def wado_frames(
    study: StudyAccessPolicy,
    series_uid: str,
    sop_uid: str,
    frame_list: str,
    store: MetadataStoreDep,
    object_store: ObjectStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    body, boundary = await retrieve_frames(
        study, series_uid, sop_uid, frame_list, store, object_store, user
    )
    return _multipart_response(body, boundary, "application/octet-stream")


@router.get(
    "/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def wado_metadata(
    study: StudyAccessPolicy,
    series_uid: str,
    sop_uid: str,
    store: MetadataStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return await retrieve_instance_metadata(study, series_uid, sop_uid, store, user)


@router.get(
    "/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/rendered",
    dependencies=[Depends(require_capability(Capability.STUDY_READ))],
)
async def wado_rendered(
    study: StudyAccessPolicy,
    series_uid: str,
    sop_uid: str,
    store: MetadataStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    data = await retrieve_rendered(study, series_uid, sop_uid, store, user)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store"},
    )


# ---------------------------------------------------------------------------
# STOW-RS
# ---------------------------------------------------------------------------
@router.post(
    "/studies",
    dependencies=[Depends(require_capability(Capability.STUDY_IMPORT))],
)
async def stow(
    request: Request,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    dataset, http_status = await store_instances(
        object_store,
        user,
        body,
        content_type,
        quarantine_bucket=settings.quarantine_bucket_name,
    )
    return stow_json_response(dataset, http_status)
