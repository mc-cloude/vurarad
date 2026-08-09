# ruff: noqa: B008
"""Findings router — routes 20-21 (§3.15, §3.22).

- ``GET /studies/{studyId}/findings`` (study:read) — list findings.
- ``POST /studies/{studyId}/findings/{findingId}/disposition`` (study:annotate,
  MFA, Idempotency-Key) — apply a disposition.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.v1.routers.studies_deps import ViewerScopeDep, require_phi_capability
from app.api.v1.routers.wp12_deps import FindingServiceDep, StudyRecordDep
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.models.finding import DispositionRequest, Finding, FindingsResponse

router = APIRouter(
    prefix="/studies",
    tags=["findings"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


def _require_idempotency_key(key: str | None) -> str:
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Idempotency-Key header is required",
                }
            },
        )
    return key


# ---------------------------------------------------------------------------
# GET /studies/{studyId}/findings — route 20
# ---------------------------------------------------------------------------
@router.get(
    "/{study_id}/findings",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_READ))],
    response_model=FindingsResponse,
)
async def list_findings(
    study: StudyRecordDep,
    finding_service: FindingServiceDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> FindingsResponse:
    """List all findings for a study (§3.15.1)."""
    return await finding_service.list_findings(user, study, viewer_scope)


# ---------------------------------------------------------------------------
# POST /studies/{studyId}/findings/{findingId}/disposition — route 21
# ---------------------------------------------------------------------------
@router.post(
    "/{study_id}/findings/{finding_id}/disposition",
    dependencies=[Depends(require_phi_capability(Capability.STUDY_ANNOTATE))],
    response_model=Finding,
)
async def disposition_finding(
    body: DispositionRequest,
    study: StudyRecordDep,
    finding_id: str,
    finding_service: FindingServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Finding:
    """Apply a disposition to a finding (§3.15.2).

    Requires ``confirmedText`` for ``CONFIRMED`` / ``EDITED`` (enforced in the
    model).  Writes a ``FINDING_DISPOSITIONED`` audit event.
    """
    _require_idempotency_key(idempotency_key)
    return await finding_service.disposition(user, study, finding_id, body)
