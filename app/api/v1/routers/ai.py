# ruff: noqa: B008
"""AI router — SSE streaming for report drafting and study Q&A (§3.21 AI).

Routes:

- ``POST /studies/{studyId}/ai/report-draft`` (``ai:draft``) — streams a
  structured report draft built from the radiologist's dictation and the
  confirmed findings.  SSE shape: ``meta → delta(s) → done`` or ``meta → error``.
- ``POST /studies/{studyId}/ai/qa`` (``ai:full``) — streams an answer to a
  radiologist's question about the study.

Security:

- Both capabilities are PHI capabilities, so ``require_phi_capability`` returns
  ``403 PHI_ACCESS_FORBIDDEN`` for admin (who structurally holds zero PHI
  capabilities) — acceptance criterion 18.
- Every route carries ``get_current_user`` and ``require_mfa`` at the router
  level.
- Per-user rate limiting (the ``ai`` bucket) is enforced **before** the stream
  starts; the 11th request in a window returns ``429`` with ``Retry-After``
  (criterion 17).
- The prompt is built from a PHI allow-list (:func:`build_prompt_input`); the
  only text sent upstream is the rendered allow-list + dictation (criterion 13).
- Budget is checked inside the stream before any SDK call (criterion 12); a
  refusal is an ``error`` frame with ``AI_BUDGET_EXCEEDED``.
- A timeout is an ``error`` frame with ``AI_UPSTREAM_TIMEOUT`` (criterion 16).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.v1.routers.studies_deps import (
    DocumentStoreDep,
    require_phi_capability,
)
from app.api.v1.routers.wp12_deps import StudyRecordDep
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.core.config import Settings
from app.core.errors import AiRateLimitedError
from app.core.ratelimit import RateLimitBucket, RateLimiter
from app.models.ai import AiQaRequest, PromptInput
from app.models.errors import ErrorCode
from app.models.study import PriorStudyRef, StudyRecord
from app.repositories.ai_budget_repo import AiBudgetRepo
from app.repositories.base import DocumentStore
from app.services.dictation_service import DICTATION_SEGMENTS_COLLECTION
from app.services.finding_service import FINDINGS_COLLECTION
from app.services.gemini_service import GeminiService
from app.services.prompt_builder import (
    age_days_from_age_sex,
    build_prompt_input,
    render_prompt_contents,
)

router = APIRouter(
    prefix="/studies",
    tags=["ai"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

# Module-level fallback rate limiter; tests inject a fresh one on app.state.
_FALLBACK_RATE_LIMITER = RateLimiter()


# ---------------------------------------------------------------------------
# Dependency providers (inline — no separate _deps module for WP6)
# ---------------------------------------------------------------------------
async def get_gemini_service(request: Request) -> GeminiService:
    """Return the :class:`GeminiService` from ``app.state`` or build a real one.

    The production service is built with an :class:`AiBudgetRepo` so the monthly
    spend ceiling is enforced inside the stream (before any SDK call).  Tests
    inject a pre-built service (with a fake client + fake budget repo) on
    ``app.state.gemini_service``.
    """
    service = getattr(request.app.state, "gemini_service", None)
    if service is not None:
        return service  # type: ignore[no-any-return]
    settings: Settings = request.app.state.settings
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        from app.repositories.base import FirestoreDocumentStore

        store = FirestoreDocumentStore.from_settings(settings)
    return GeminiService(
        project=settings.gcp_project_id,
        location=settings.vertex_location,
        budget_repo=AiBudgetRepo(store),
    )


GeminiServiceDep = Annotated[GeminiService, Depends(get_gemini_service)]


async def get_rate_limiter(request: Request) -> RateLimiter:
    """Return the :class:`RateLimiter` from ``app.state`` or a module singleton."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        return limiter  # type: ignore[no-any-return]
    return _FALLBACK_RATE_LIMITER


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


# ---------------------------------------------------------------------------
# Helpers — confirmed findings, dictation, priors, prompt loading
# ---------------------------------------------------------------------------
async def _confirmed_findings(store: DocumentStore, study_id: str) -> list[str]:
    """Return the ``confirmedText`` of every CONFIRMED finding for the study.

    Only the confirmed text of a CONFIRMED/EDITED finding may enter a report
    (§3.15.2).  PENDING/REJECTED findings are excluded.
    """
    rows = await store.query(
        FINDINGS_COLLECTION,
        where=[("study_id", "==", study_id)],
    )
    texts: list[str] = []
    for _doc_id, doc in rows:
        disposition = doc.get("disposition")
        if not isinstance(disposition, dict):
            continue
        if disposition.get("state") != "CONFIRMED":
            continue
        text = disposition.get("confirmed_text") or disposition.get("confirmedText")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


async def _dictation_text(store: DocumentStore, study_id: str) -> str:
    """Return the concatenated dictation narrative for the study, ordered by ``at``."""
    rows = await store.query(
        DICTATION_SEGMENTS_COLLECTION,
        where=[("study_id", "==", study_id)],
    )
    segments: list[tuple[Any, str]] = []
    for _doc_id, doc in rows:
        at = doc.get("at", "")
        text = doc.get("text", "")
        if isinstance(text, str):
            segments.append((at, text))
    segments.sort(key=lambda item: str(item[0]))
    return "\n".join(text for _at, text in segments)


def _priors_summary(study: StudyRecord) -> str:
    """Build a de-identified prior-studies summary from the study record."""
    if not study.prior_studies:
        return ""
    lines: list[str] = []
    for prior in study.prior_studies:
        lines.append(_prior_line(prior))
    return "\n".join(lines)


def _prior_line(prior: PriorStudyRef) -> str:
    parts = [prior.modality, prior.body_part, prior.study_date, prior.description]
    return ", ".join(p for p in parts if p)


def _load_prompt(filename: str) -> str:
    """Read a version-stamped prompt template from ``app/prompts/``."""
    return (_PROMPTS_DIR / filename).read_text()


def _rate_limited(limiter: RateLimiter, user: AuthenticatedUser) -> JSONResponse | None:
    """Return a 429 ``Retry-After`` response if the user is over the AI bucket, else None."""
    result = limiter.check(user.uid, RateLimitBucket.AI)
    if result.allowed:
        return None
    retry_after = max(1, int(result.retry_after))
    return JSONResponse(
        status_code=AiRateLimitedError.status_code,
        headers={"Retry-After": str(retry_after)},
        content={
            "error": {
                "code": ErrorCode.AI_RATE_LIMITED.value,
                "message": "AI request rate limit exceeded",
                "requestId": uuid.uuid4().hex[:12],
            }
        },
    )


def _sse_response(stream: Any) -> StreamingResponse:
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _build_prompt_input(
    study: StudyRecord,
    *,
    confirmed: list[str],
    template_id: str | None,
    priors_summary: str | None,
) -> PromptInput:
    return build_prompt_input(
        study,
        confirmed_findings=confirmed,
        patient_age_days=age_days_from_age_sex(study.patient_age_sex),
        template_id=template_id,
        priors_summary=priors_summary,
    )


# ---------------------------------------------------------------------------
# POST /studies/{studyId}/ai/report-draft
# ---------------------------------------------------------------------------
@router.post(
    "/{study_id}/ai/report-draft",
    response_model=None,
    dependencies=[Depends(require_phi_capability(Capability.AI_DRAFT))],
)
async def report_draft(
    study: StudyRecordDep,
    doc_store: DocumentStoreDep,
    gemini_service: GeminiServiceDep,
    rate_limiter: RateLimiterDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse | JSONResponse:
    """Stream a structured report draft from dictation + confirmed findings."""
    limited = _rate_limited(rate_limiter, user)
    if limited is not None:
        return limited

    confirmed = await _confirmed_findings(doc_store, study.study_id)
    dictation = await _dictation_text(doc_store, study.study_id)
    prompt_input = _build_prompt_input(
        study,
        confirmed=confirmed,
        template_id=None,
        priors_summary=_priors_summary(study) or None,
    )
    contents = render_prompt_contents(prompt_input, dictation)
    system_instruction = _load_prompt("report_draft.md")
    request_id = uuid.uuid4().hex[:12]
    stream = gemini_service.generate_stream(
        request_id=request_id,
        contents=contents,
        system_instruction=system_instruction,
        tenant_id=study.tenant_id,
    )
    return _sse_response(stream)


# ---------------------------------------------------------------------------
# POST /studies/{studyId}/ai/qa
# ---------------------------------------------------------------------------
@router.post(
    "/{study_id}/ai/qa",
    response_model=None,
    dependencies=[Depends(require_phi_capability(Capability.AI_FULL))],
)
async def study_qa(
    body: AiQaRequest,
    study: StudyRecordDep,
    doc_store: DocumentStoreDep,
    gemini_service: GeminiServiceDep,
    rate_limiter: RateLimiterDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse | JSONResponse:
    """Stream an answer to a radiologist's question about the study."""
    limited = _rate_limited(rate_limiter, user)
    if limited is not None:
        return limited

    confirmed = await _confirmed_findings(doc_store, study.study_id)
    dictation = await _dictation_text(doc_store, study.study_id)
    prompt_input = _build_prompt_input(
        study,
        confirmed=confirmed,
        template_id=body.template_id,
        priors_summary=body.priors_summary or _priors_summary(study) or None,
    )
    contents = render_prompt_contents(prompt_input, dictation)
    contents += f"\n[QUESTION]\n{body.question}\n"
    system_instruction = _load_prompt("qa.md")
    request_id = uuid.uuid4().hex[:12]
    stream = gemini_service.generate_stream(
        request_id=request_id,
        contents=contents,
        system_instruction=system_instruction,
        tenant_id=study.tenant_id,
    )
    return _sse_response(stream)
