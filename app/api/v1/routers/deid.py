# ruff: noqa: B008
"""De-identification routes — the 4 de-ID endpoints (§3.17.1).

All four routes are gated by the ``deid:review`` capability and a verified
second factor.  ``deid:review`` is a PHI capability held by no default role and
never by admin, so the de-ID surface (and the ``deid_links`` provenance map it
writes) is unreachable from ``cohort:*`` capabilities (criterion 7).

Routes:
* ``POST /deid/runs``           — de-identify one DICOM instance.
* ``GET  /deid/runs/{runId}``   — list the review items produced by a run.
* ``GET  /deid/reviews``        — list pending review items.
* ``POST /deid/reviews/{reviewId}/resolve`` — resolve a review item.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.v1.routers.deid_deps import DeidServiceDep
from app.core.auth import AuthenticatedUser, get_current_user, require_capability, require_mfa
from app.core.capabilities import Capability
from app.models.common import CamelModel
from app.repositories.deid_repo import DeidReview, ReviewStatus
from app.services.deid.pipeline import DeidResult

router = APIRouter(tags=["deid"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class DeidRunRequest(CamelModel):
    """Body for ``POST /deid/runs``."""

    study_id: str
    series_id: str
    source_object_path: str
    original_sop_instance_uid: str
    modality: str
    manufacturer: str


class ResolveReviewRequest(CamelModel):
    """Body for ``POST /deid/reviews/{reviewId}/resolve``."""

    status: ReviewStatus
    note: str = ""


class DeidRegionResponse(CamelModel):
    bbox: list[int]
    text: str
    label: str
    confidence: float
    decision: str
    reason: str
    frame_index: int = 0


class DeidRunResponse(CamelModel):
    """The outcome of one de-ID run (criterion 11 fields included)."""

    run_id: str
    study_id: str
    series_id: str
    modality: str
    manufacturer: str
    tag_profile_version: str
    ocr_engine_version: str
    phi_model_id: str
    phi_model_revision: str
    phi_tag_found: bool
    unvalidated_source: bool
    regions_total: int
    regions_redacted: int
    regions_kept: int
    regions_review: int
    new_sop_instance_uid: str | None
    completed: bool
    redacted_object_path: str | None
    review_item_ids: list[str] = []
    error: str | None = None


class DeidRunSummaryResponse(CamelModel):
    run_id: str
    reviews: list[DeidReview]
    review_count: int


class DeidReviewListResponse(CamelModel):
    items: list[DeidReview]
    next_cursor: str | None = None


class DeidReviewResolveResponse(CamelModel):
    review: DeidReview


_DEID_DEPS = [
    Depends(require_capability(Capability.DEID_REVIEW)),
    Depends(require_mfa),
]


def _to_run_response(result: DeidResult) -> DeidRunResponse:
    return DeidRunResponse(
        run_id=result.run_id,
        study_id=result.study_id,
        series_id=result.series_id,
        modality=result.modality,
        manufacturer=result.manufacturer,
        tag_profile_version=result.tag_profile_version,
        ocr_engine_version=result.ocr_engine_version,
        phi_model_id=result.phi_model_id,
        phi_model_revision=result.phi_model_revision,
        phi_tag_found=result.phi_tag_found,
        unvalidated_source=result.unvalidated_source,
        regions_total=result.regions_total,
        regions_redacted=result.regions_redacted,
        regions_kept=result.regions_kept,
        regions_review=result.regions_review,
        new_sop_instance_uid=result.new_sop_instance_uid,
        completed=result.completed,
        redacted_object_path=result.redacted_object_path,
        review_item_ids=result.review_item_ids,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("/deid/runs", dependencies=_DEID_DEPS)
async def create_deid_run(
    req: DeidRunRequest,
    service: DeidServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DeidRunResponse:
    """De-identify one DICOM instance (tags → OCR → OpenMed → redact → review)."""
    result = await service.run_instance(
        study_id=req.study_id,
        series_id=req.series_id,
        source_object_path=req.source_object_path,
        original_sop_instance_uid=req.original_sop_instance_uid,
        modality=req.modality,
        manufacturer=req.manufacturer,
        actor=user.uid,
        second_factor=user.is_mfa_verified,
    )
    return _to_run_response(result)


@router.get("/deid/runs/{run_id}", dependencies=_DEID_DEPS)
async def get_deid_run(
    run_id: str,
    service: DeidServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DeidRunSummaryResponse:
    """List the review items produced by a de-ID run."""
    reviews = await service.list_run_reviews(run_id)
    return DeidRunSummaryResponse(run_id=run_id, reviews=reviews, review_count=len(reviews))


@router.get("/deid/reviews", dependencies=_DEID_DEPS)
async def list_deid_reviews(
    service: DeidServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=1000),
) -> DeidReviewListResponse:
    """List pending de-ID review items."""
    items = await service.list_reviews(limit)
    return DeidReviewListResponse(items=items)


@router.post("/deid/reviews/{review_id}/resolve", dependencies=_DEID_DEPS)
async def resolve_deid_review(
    review_id: str,
    req: ResolveReviewRequest,
    service: DeidServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DeidReviewResolveResponse:
    """Resolve a de-ID review item (approve / reject / restore)."""
    review = await service.resolve_review(review_id, req.status, actor=user.uid, note=req.note)
    return DeidReviewResolveResponse(review=review)
