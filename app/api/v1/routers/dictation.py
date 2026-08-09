# ruff: noqa: B008
"""Dictation router — routes 27-29 (§3.21.3, §3.22).

- ``POST /dictation/sessions`` (report:write, MFA, Idempotency-Key) — start.
- ``POST /dictation/sessions/{sessionId}/segments`` (report:write, MFA,
  Idempotency-Key) — append a segment (idempotent on mutationId).
- ``GET /dictation/sessions/{sessionId}`` (report:write, MFA) — get ordered
  segments.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.v1.routers.studies_deps import require_phi_capability
from app.api.v1.routers.wp12_deps import DictationServiceDep
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.models.dictation import (
    DictationSegment,
    DictationSegmentCreate,
    DictationSession,
    DictationSessionCreate,
    DictationSessionResponse,
)

router = APIRouter(
    prefix="/dictation",
    tags=["dictation"],
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
# POST /dictation/sessions — route 27
# ---------------------------------------------------------------------------
@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_phi_capability(Capability.REPORT_WRITE))],
    response_model=DictationSession,
)
async def start_session(
    body: DictationSessionCreate,
    dictation_service: DictationServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DictationSession:
    """Start a dictation session scoped to a study."""
    _require_idempotency_key(idempotency_key)
    return await dictation_service.start_session(user, body)


# ---------------------------------------------------------------------------
# POST /dictation/sessions/{sessionId}/segments — route 28
# ---------------------------------------------------------------------------
@router.post(
    "/sessions/{session_id}/segments",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_phi_capability(Capability.REPORT_WRITE))],
    response_model=DictationSegment,
)
async def append_segment(
    body: DictationSegmentCreate,
    session_id: str,
    dictation_service: DictationServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DictationSegment:
    """Append one segment (idempotent on mutationId)."""
    _require_idempotency_key(idempotency_key)
    return await dictation_service.append_segment(user, session_id, body)


# ---------------------------------------------------------------------------
# GET /dictation/sessions/{sessionId} — route 29
# ---------------------------------------------------------------------------
@router.get(
    "/sessions/{session_id}",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_WRITE))],
    response_model=DictationSessionResponse,
)
async def get_session(
    session_id: str,
    dictation_service: DictationServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DictationSessionResponse:
    """Return a session with its segments ordered by ``at``."""
    return await dictation_service.get_session(user, session_id)
