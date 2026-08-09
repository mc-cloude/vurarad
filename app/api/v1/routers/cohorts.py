# ruff: noqa: B008
"""Cohort routes — 60-63 (§3.22, WP17).

- ``POST /cohorts`` (``cohort:create``) — create a cohort (IRB reference +
  determination required).
- ``GET /cohorts`` (``cohort:read``) — list cohorts.
- ``GET /cohorts/{cohortId}`` (``cohort:read``) — one cohort.
- ``POST /cohorts/{cohortId}/subjects`` (``cohort:subject:add``) — add a subject
  through the de-identification pipeline.
- ``POST /cohorts/{cohortId}/segmentation`` (``cohort:segmentation``) — create or
  extend a versioned segmentation.

Every route carries ``get_current_user`` + ``require_mfa`` at the router level
and a ``cohort:*`` capability at the route level.  The ``researcher`` role holds
the ``cohort:*`` capabilities and zero clinical capabilities, so a clinical
radiologist cannot reach these routes — the research/clinical barrier at the
RBAC layer (criterion 8).  No response model carries ``studyId`` / ``patientKey``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.routers.studies_deps import (
    AuditServiceDep,
    DocumentStoreDep,
    SettingsDep,
    StudyRepoDep,
)
from app.core.auth import AuthenticatedUser, get_current_user, require_capability, require_mfa
from app.core.capabilities import Capability
from app.models.cohort import (
    AddSubjectRequest,
    Cohort,
    CohortSegmentation,
    CohortSubject,
    CreateCohortRequest,
    CreateSegmentationRequest,
)
from app.repositories.cohort_repo import CohortRepository
from app.repositories.deid_link_repo import DeidLinkRepository
from app.services.cohort_segmentation_service import (
    CohortSegmentationService,
    ResearchSegmenter,
    StubResearchSegmenter,
)
from app.services.cohort_service import CohortService
from app.services.cohort_subject_service import CohortSubjectService
from app.services.deid_pipeline import DeidPipeline, StubDeidPipeline
from app.storage.base import ObjectStore

router = APIRouter(
    prefix="/cohorts",
    tags=["cohorts"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------
async def get_cohort_repo(doc_store: DocumentStoreDep) -> CohortRepository:
    return CohortRepository(doc_store)


CohortRepoDep = Annotated[CohortRepository, Depends(get_cohort_repo)]


async def get_deid_link_repo(doc_store: DocumentStoreDep) -> DeidLinkRepository:
    return DeidLinkRepository(doc_store)


DeidLinkRepoDep = Annotated[DeidLinkRepository, Depends(get_deid_link_repo)]


async def get_deid_pipeline(
    request: Request,
    deid_link_repo: DeidLinkRepoDep,
) -> DeidPipeline:
    pipeline = getattr(request.app.state, "deid_pipeline", None)
    if pipeline is None:
        pipeline = StubDeidPipeline(deid_link_repo)
        request.app.state.deid_pipeline = pipeline
    return pipeline


DeidPipelineDep = Annotated[DeidPipeline, Depends(get_deid_pipeline)]


async def get_research_segmenter(request: Request) -> ResearchSegmenter:
    segmenter = getattr(request.app.state, "research_segmenter", None)
    if segmenter is None:
        segmenter = StubResearchSegmenter()
        request.app.state.research_segmenter = segmenter
    return segmenter


SegmenterDep = Annotated[ResearchSegmenter, Depends(get_research_segmenter)]


async def get_optional_object_store(request: Request) -> ObjectStore | None:
    return getattr(request.app.state, "object_store", None)


OptionalObjectStoreDep = Annotated[ObjectStore | None, Depends(get_optional_object_store)]


async def get_cohort_service(
    cohort_repo: CohortRepoDep,
    audit_service: AuditServiceDep,
) -> CohortService:
    return CohortService(cohort_repo, audit_service)


CohortServiceDep = Annotated[CohortService, Depends(get_cohort_service)]


async def get_cohort_subject_service(
    cohort_repo: CohortRepoDep,
    deid_pipeline: DeidPipelineDep,
    doc_store: DocumentStoreDep,
    audit_service: AuditServiceDep,
    study_repo: StudyRepoDep,
) -> CohortSubjectService:
    return CohortSubjectService(
        cohort_repo,
        deid_pipeline,
        doc_store,
        audit_service,
        study_repo=study_repo,
    )


CohortSubjectServiceDep = Annotated[CohortSubjectService, Depends(get_cohort_subject_service)]


async def get_cohort_segmentation_service(
    cohort_repo: CohortRepoDep,
    segmenter: SegmenterDep,
    audit_service: AuditServiceDep,
    object_store: OptionalObjectStoreDep,
    settings: SettingsDep,
) -> CohortSegmentationService:
    return CohortSegmentationService(
        cohort_repo,
        segmenter,
        audit_service,
        object_store,
        deid_bucket=settings.deid_bucket_name or "deid",
    )


CohortSegmentationServiceDep = Annotated[
    CohortSegmentationService, Depends(get_cohort_segmentation_service)
]


# ---------------------------------------------------------------------------
# POST /cohorts — route 60
# ---------------------------------------------------------------------------
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_capability(Capability.COHORT_CREATE))],
)
async def create_cohort(
    body: CreateCohortRequest,
    cohort_service: CohortServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Cohort:
    return await cohort_service.create_cohort(
        user,
        name=body.name,
        description=body.description,
        irb_reference=body.irb_reference,
        irb_determination=body.irb_determination,
        retention_days=body.retention_days,
    )


# ---------------------------------------------------------------------------
# GET /cohorts — route 61
# ---------------------------------------------------------------------------
@router.get(
    "",
    dependencies=[Depends(require_capability(Capability.COHORT_READ))],
)
async def list_cohorts(
    cohort_service: CohortServiceDep,
) -> JSONResponse:
    cohorts = await cohort_service.list_cohorts()
    return JSONResponse(
        content={"items": [c.model_dump(by_alias=True) for c in cohorts]}
    )


# ---------------------------------------------------------------------------
# GET /cohorts/{cohortId} — route 61
# ---------------------------------------------------------------------------
@router.get(
    "/{cohort_id}",
    dependencies=[Depends(require_capability(Capability.COHORT_READ))],
)
async def get_cohort(
    cohort_id: str,
    cohort_service: CohortServiceDep,
) -> Cohort:
    return await cohort_service.get_cohort(cohort_id)


# ---------------------------------------------------------------------------
# POST /cohorts/{cohortId}/subjects — route 62
# ---------------------------------------------------------------------------
@router.post(
    "/{cohort_id}/subjects",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_capability(Capability.COHORT_SUBJECT_ADD))],
)
async def add_subject(
    cohort_id: str,
    body: AddSubjectRequest,
    subject_service: CohortSubjectServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CohortSubject:
    if body.source_kind == "WORKLIST":
        return await subject_service.add_from_worklist(user, cohort_id, body.study_id)
    return await subject_service.add_from_upload(
        user,
        cohort_id,
        body.upload_ref,
        modality=body.modality,
        body_part=body.body_part,
    )


# ---------------------------------------------------------------------------
# POST /cohorts/{cohortId}/segmentation — route 63
# ---------------------------------------------------------------------------
@router.post(
    "/{cohort_id}/segmentation",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_capability(Capability.COHORT_SEGMENTATION))],
)
async def create_segmentation(
    cohort_id: str,
    body: CreateSegmentationRequest,
    segmentation_service: CohortSegmentationServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CohortSegmentation:
    return await segmentation_service.create_or_edit_segmentation(
        user,
        cohort_id,
        body.subject_id,
        source=body.source,
        bundle_id=body.bundle_id,
    )
