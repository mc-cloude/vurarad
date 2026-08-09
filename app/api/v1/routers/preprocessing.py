# ruff: noqa: B008
"""Pre-processing router — routes 18-19, 41 (§3.22).

- ``GET /studies/{studyId}/preprocessing`` (study:read, MFA) — pipeline state.
- ``POST /studies/{studyId}/preprocessing/retry`` (study:import, MFA) — retry a
  stage.
- ``GET /capabilities/segmentation`` (authenticated) — registry + runtime state.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.v1.routers.studies_deps import require_phi_capability
from app.api.v1.routers.wp12_deps import (
    DispatcherDep,
    OrchestratorDep,
    PreprocessingRegistryDep,
    StudyRecordDep,
)
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.models.preprocessing import PreprocessingStageName, PreprocessingState

router = APIRouter(tags=["preprocessing"])


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------
class PreprocessingRetryRequest(BaseModel):
    """Request body for ``POST /studies/{id}/preprocessing/retry``."""

    stage: PreprocessingStageName


class SegmentationCapabilityEntry(BaseModel):
    """One entry in the segmentation capability response (§3.21.4)."""

    modality: str
    body_part: str
    specialisation: str = "*"
    bundle_id: str | None = None
    state: str
    regulatory_class: str = "RUO"
    expected_seconds: int = 0
    detail: str = ""


class SegmentationCapabilityResponse(BaseModel):
    """Response for ``GET /capabilities/segmentation`` (§3.21.4)."""

    runtime: str
    region: str
    entries: list[SegmentationCapabilityEntry]


# ---------------------------------------------------------------------------
# GET /studies/{studyId}/preprocessing — route 18
# ---------------------------------------------------------------------------
@router.get(
    "/studies/{study_id}/preprocessing",
    dependencies=[
        Depends(get_current_user),
        Depends(require_mfa),
        Depends(require_phi_capability(Capability.STUDY_READ)),
    ],
    response_model=PreprocessingState,
)
async def get_preprocessing_state(
    study: StudyRecordDep,
    orchestrator: OrchestratorDep,
) -> PreprocessingState:
    """Return the pre-processing state for a study (never blocks reading)."""
    return await orchestrator.get_state(study.study_id)


# ---------------------------------------------------------------------------
# POST /studies/{studyId}/preprocessing/retry — route 19
# ---------------------------------------------------------------------------
@router.post(
    "/studies/{study_id}/preprocessing/retry",
    dependencies=[
        Depends(get_current_user),
        Depends(require_mfa),
        Depends(require_phi_capability(Capability.STUDY_IMPORT)),
    ],
    response_model=PreprocessingState,
)
async def retry_preprocessing_stage(
    body: PreprocessingRetryRequest,
    study: StudyRecordDep,
    orchestrator: OrchestratorDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PreprocessingState:
    """Retry a single pre-processing stage (idempotent)."""
    return await orchestrator.retry_stage(study, body.stage, user)


# ---------------------------------------------------------------------------
# GET /capabilities/segmentation — route 41
# ---------------------------------------------------------------------------
@router.get(
    "/capabilities/segmentation",
    dependencies=[Depends(get_current_user)],
    response_model=SegmentationCapabilityResponse,
)
async def get_segmentation_capabilities(
    registry: PreprocessingRegistryDep,
    dispatcher: DispatcherDep,
) -> SegmentationCapabilityResponse:
    """Return the effective segmentation capability per modality/bodyPart."""
    entries: list[SegmentationCapabilityEntry] = []
    for entry in registry.entries():
        plan = dispatcher.resolve_modalities(entry.modality, entry.body_part)
        entries.append(
            SegmentationCapabilityEntry(
                modality=entry.modality,
                body_part=entry.body_part,
                specialisation=entry.specialisation,
                bundle_id=plan.bundle_id,
                state=plan.state.value,
                regulatory_class=plan.regulatory_class,
                expected_seconds=plan.expected_seconds,
                detail=plan.detail,
            )
        )
    return SegmentationCapabilityResponse(
        runtime=dispatcher._runtime,  # noqa: SLF001
        region=dispatcher._region,  # noqa: SLF001
        entries=entries,
    )
