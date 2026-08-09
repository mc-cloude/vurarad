# ruff: noqa: B008
"""Report routes — lifecycle, signing, addenda, version history (WP5).

- ``POST /studies/{studyId}/reports`` — create a draft (study:write).
- ``GET /reports/{reportId}`` — read a report (report:read).
- ``PATCH /reports/{reportId}`` — update a draft (report:write).
- ``POST /reports/{reportId}/sign`` — sign (report:sign).
- ``POST /reports/{reportId}/addenda`` — addendum (report:write).
- ``GET /reports/{reportId}/versions`` — list versions (report:read).
- ``GET /reports/{reportId}/versions/{version}`` — read a version (report:read).

Every route carries ``get_current_user`` + ``require_mfa`` at the router level and
a ``require_phi_capability`` per route, so admin gets ``403 PHI_ACCESS_FORBIDDEN``
on every route (admin holds zero PHI capabilities) and a first-factor-only token
gets ``403 MFA_REQUIRED``.  Mutations additionally require an Idempotency-Key;
signing requires a fresh ``X-Second-Factor-Assertion`` and an attestation.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from app.api.v1.routers.studies_deps import (
    AuditMirrorDep,
    AuditServiceDep,
    DocumentStoreDep,
    SettingsDep,
    StudyRepoDep,
    ViewerScopeDep,
    require_phi_capability,
)
from app.api.v1.routers.wp12_deps import StudyRecordDep
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.core.errors import IdempotencyKeyRequiredError
from app.models.report import (
    AddendumRequest,
    CreateReportRequest,
    ReportResponse,
    ReportVersionResponse,
    SignReportRequest,
    UpdateReportRequest,
)
from app.repositories.report_repo import ReportRepo
from app.repositories.version_repo import VersionRepo
from app.services.analytics_service import AnalyticsService
from app.services.report_service import (
    InMemoryCounterStore,
    ReportService,
    SecondFactorAssertionStore,
    SignIdempotencyCache,
)

router = APIRouter(
    tags=["reports"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


# ---------------------------------------------------------------------------
# Dependency providers — composition root for the report service
# ---------------------------------------------------------------------------
def _get_or_create[T](request: Request, attr: str, factory: type[T]) -> T:
    """Return a singleton on ``app.state``; lazily build + cache it."""
    value = getattr(request.app.state, attr, None)
    if value is None:
        value = factory()
        setattr(request.app.state, attr, value)
    return cast(T, value)


async def get_report_service(
    request: Request,
    doc_store: DocumentStoreDep,
    study_repo: StudyRepoDep,
    audit_service: AuditServiceDep,
    audit_mirror: AuditMirrorDep,
    settings: SettingsDep,
) -> ReportService:
    analytics_store = _get_or_create(request, "analytics_counter_store", InMemoryCounterStore)
    idem_cache = _get_or_create(request, "sign_idempotency_cache", SignIdempotencyCache)
    assertion_store = _get_or_create(
        request, "second_factor_assertion_store", SecondFactorAssertionStore
    )
    version_repo = VersionRepo(doc_store)
    report_repo = ReportRepo(
        doc_store,
        version_repo,
        audit_mirror=audit_mirror,
        analytics_store=analytics_store,
    )
    analytics = AnalyticsService(analytics_store)
    return ReportService(
        report_repo,
        version_repo,
        study_repo,
        audit_service,
        analytics,
        idem_cache,
        assertion_store,
        mfa_freshness_seconds=settings.mfa_verification_seconds,
    )


ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]


def _require_idempotency_key(key: str | None) -> str:
    """Raise ``IdempotencyKeyRequiredError`` (400) for a missing/empty key."""
    if not key:
        raise IdempotencyKeyRequiredError()
    return key


# ---------------------------------------------------------------------------
# POST /studies/{studyId}/reports — create a draft
# ---------------------------------------------------------------------------
@router.post(
    "/studies/{study_id}/reports",
    status_code=201,
    dependencies=[Depends(require_phi_capability(Capability.STUDY_WRITE))],
    response_model=ReportResponse,
)
async def create_report(
    study_id: str,
    body: CreateReportRequest,
    study: StudyRecordDep,
    report_service: ReportServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReportResponse:
    """Create a new draft report for a study (radiologist must be assigned)."""
    return await report_service.create_draft(user, study, body)


# ---------------------------------------------------------------------------
# GET /reports/{reportId} — read a report
# ---------------------------------------------------------------------------
@router.get(
    "/reports/{report_id}",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_READ))],
    response_model=ReportResponse,
)
async def get_report(
    report_id: str,
    report_service: ReportServiceDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReportResponse:
    return await report_service.get_report(user, report_id, viewer_scope)


# ---------------------------------------------------------------------------
# PATCH /reports/{reportId} — update a draft
# ---------------------------------------------------------------------------
@router.patch(
    "/reports/{report_id}",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_WRITE))],
    response_model=ReportResponse,
)
async def update_report(
    report_id: str,
    body: UpdateReportRequest,
    report_service: ReportServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReportResponse:
    return await report_service.update_draft(user, report_id, body)


# ---------------------------------------------------------------------------
# POST /reports/{reportId}/sign — sign (fresh 2FA + attestation + idempotency)
# ---------------------------------------------------------------------------
@router.post(
    "/reports/{report_id}/sign",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_SIGN))],
)
async def sign_report(
    report_id: str,
    body: SignReportRequest,
    report_service: ReportServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    second_factor_assertion: str | None = Header(default=None, alias="X-Second-Factor-Assertion"),
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    response_body, status_code = await report_service.sign(
        user, report_id, body, key, second_factor_assertion
    )
    return JSONResponse(content=response_body, status_code=status_code)


# ---------------------------------------------------------------------------
# POST /reports/{reportId}/addenda — signed addendum to a SIGNED parent
# ---------------------------------------------------------------------------
@router.post(
    "/reports/{report_id}/addenda",
    status_code=201,
    dependencies=[Depends(require_phi_capability(Capability.REPORT_WRITE))],
)
async def create_addendum(
    report_id: str,
    body: AddendumRequest,
    report_service: ReportServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    second_factor_assertion: str | None = Header(default=None, alias="X-Second-Factor-Assertion"),
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    response_body, status_code = await report_service.create_addendum(
        user, report_id, body, key, second_factor_assertion
    )
    return JSONResponse(content=response_body, status_code=status_code)


# ---------------------------------------------------------------------------
# GET /reports/{reportId}/versions — version history (ascending)
# ---------------------------------------------------------------------------
@router.get(
    "/reports/{report_id}/versions",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_READ))],
    response_model=list[ReportVersionResponse],
)
async def list_versions(
    report_id: str,
    report_service: ReportServiceDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ReportVersionResponse]:
    return await report_service.get_versions(user, report_id, viewer_scope)


# ---------------------------------------------------------------------------
# GET /reports/{reportId}/versions/{version} — read a single version
# ---------------------------------------------------------------------------
@router.get(
    "/reports/{report_id}/versions/{version}",
    dependencies=[Depends(require_phi_capability(Capability.REPORT_READ))],
    response_model=ReportVersionResponse,
)
async def get_version(
    report_id: str,
    version: int,
    report_service: ReportServiceDep,
    viewer_scope: ViewerScopeDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReportVersionResponse:
    return await report_service.get_version(user, report_id, version, viewer_scope)
