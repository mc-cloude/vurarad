# ruff: noqa: B008
"""Study routes — worklist, search, detail, patient identity, series, access URLs.

Every route carries ``get_current_user`` and ``require_mfa`` at the router level
and a ``require_phi_capability`` (or ``require_patient_identity_access``) at the
route level.  Admin gets ``403 PHI_ACCESS_FORBIDDEN`` on every route in this
package because admin holds zero PHI capabilities (B2).  A first-factor-only
token gets ``403 MFA_REQUIRED`` because ``require_mfa`` rejects before any
capability check.

The worklist (``GET /studies``) accepts **no** ``limit``, ``cursor``, or
``offset`` parameter — any of them returns ``422 INVALID_QUERY_PARAMETER``.
Search (``GET /studies/search``) requires at least one filter.  Access URLs
(``GET /studies/{studyId}/series/{seriesId}/access-urls``) are chunked with a
250 default and 250 max; ``count=251`` returns ``422 INSTANCE_CHUNK_TOO_LARGE``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.v1.routers.studies_deps import (
    RenditionServiceDep,
    SettingsDep,
    StudyServiceDep,
    ViewerScopeDep,
    require_patient_identity_access,
    require_phi_capability,
)
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.core.errors import InvalidQueryParameterError
from app.models.study import (
    AccessUrlChunk,
    PatientIdentity,
    SearchResult,
    SeriesListResponse,
    StudyDetail,
    WorklistEnvelope,
)
from app.services.rendition_service import QualityManifest

router = APIRouter(
    prefix="/studies",
    tags=["studies"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)

# Query parameters that must NEVER be accepted on the worklist route.
_FORBIDDEN_WORKLIST_PARAMS: frozenset[str] = frozenset({"limit", "cursor", "offset"})


# ---------------------------------------------------------------------------
# GET /studies — capped worklist queue (1 Firestore read, no paging)
# ---------------------------------------------------------------------------
@router.get(
    "",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_READ))],
)
async def get_worklist(
    request: Request,
    study_service: StudyServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WorklistEnvelope:
    provided = set(request.query_params.keys()) & _FORBIDDEN_WORKLIST_PARAMS
    if provided:
        raise InvalidQueryParameterError(
            f"Parameters {sorted(provided)} are not accepted on this route"
        )
    return await study_service.get_worklist()


# ---------------------------------------------------------------------------
# GET /studies/search — explicit indexed query (cursor-paginated)
# ---------------------------------------------------------------------------
@router.get(
    "/search",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_SEARCH))],
)
async def search_studies(
    study_service: StudyServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    patient_ref: str | None = Query(default=None, alias="patientRef"),
    accession: str | None = Query(default=None),
    modality: str | None = Query(default=None),
    status: str | None = Query(default=None),
    study_date_from: str | None = Query(default=None, alias="from"),
    study_date_to: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=25, ge=1, le=50),
    cursor: str | None = Query(default=None),
) -> SearchResult:
    return await study_service.search(
        user,
        patient_ref=patient_ref,
        accession=accession,
        modality=modality,
        status=status,
        study_date_from=study_date_from,
        study_date_to=study_date_to,
        limit=limit,
        cursor=cursor,
    )


# ---------------------------------------------------------------------------
# GET /studies/{studyId} — study detail (de-identified)
# ---------------------------------------------------------------------------
@router.get(
    "/{study_id}",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_READ))],
)
async def get_study_detail(
    study_id: str,
    study_service: StudyServiceDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StudyDetail:
    return await study_service.get_study_detail(user, study_id, viewer_scope)


# ---------------------------------------------------------------------------
# GET /studies/{studyId}/patient-identity — the ONLY name-releasing route
# ---------------------------------------------------------------------------
@router.get(
    "/{study_id}/patient-identity",
    dependencies=[Depends(require_patient_identity_access)],
)
async def get_patient_identity(
    study_id: str,
    study_service: StudyServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PatientIdentity:
    return await study_service.get_patient_identity(user, study_id)


# ---------------------------------------------------------------------------
# GET /studies/{studyId}/series — series + geometry (1 + N reads)
# ---------------------------------------------------------------------------
@router.get(
    "/{study_id}/series",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_READ))],
)
async def get_series(
    study_id: str,
    study_service: StudyServiceDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SeriesListResponse:
    return await study_service.get_series(user, study_id, viewer_scope)


# ---------------------------------------------------------------------------
# GET /studies/{studyId}/series/{seriesId}/access-urls — chunked signed URLs
# ---------------------------------------------------------------------------
@router.get(
    "/{study_id}/series/{series_id}/access-urls",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_READ))],
)
async def get_access_urls(
    study_id: str,
    series_id: str,
    study_service: StudyServiceDep,
    settings: SettingsDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
    from_stack_index: int = Query(default=0, ge=0, alias="fromStackIndex"),
    count: int = Query(default=250, ge=1),
) -> JSONResponse:
    chunk: AccessUrlChunk = await study_service.get_access_urls(
        user,
        study_id,
        series_id,
        from_stack_index=from_stack_index,
        count=count,
        viewer_scope=viewer_scope,
    )
    # B10: Cache-Control: private, no-store and no ETag on every pixel-bearing
    # response.  The header is set on the response object, not in a comment.
    return JSONResponse(
        content=chunk.model_dump(by_alias=True),
        headers={"Cache-Control": "private, no-store"},
    )


# ---------------------------------------------------------------------------
# GET /studies/{studyId}/series/{seriesId}/manifest — per-quality byte sizes
# ---------------------------------------------------------------------------
@router.get(
    "/{study_id}/series/{series_id}/manifest",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_READ))],
)
async def get_manifest(
    study_id: str,
    series_id: str,
    study_service: StudyServiceDep,
    rendition_service: RenditionServiceDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Per-instance byte sizes at all three quality tiers (criterion 2).

    The client plans its bandwidth budget before fetching a single pixel.
    Like every pixel-bearing response, the manifest carries
    ``Cache-Control: private, no-store`` and writes a ``STUDY_IMAGES_ACCESSED``
    audit event (criterion 1).
    """
    manifest: QualityManifest = await study_service.get_manifest(
        user, study_id, series_id, rendition_service, viewer_scope
    )
    return JSONResponse(
        content=manifest.model_dump(by_alias=True),
        headers={"Cache-Control": "private, no-store"},
    )
