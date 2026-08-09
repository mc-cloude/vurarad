"""Unified error hierarchy producing the canonical wire envelope.

Every ApiError subclass maps to exactly one ErrorCode. Handlers transform
exceptions into `{"error": {code, message, requestId}}` — byte-identical
between JSON and SSE error frames.
"""

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.models.errors import ErrorCode


class ApiError(Exception):
    """Base — never raised directly."""

    code: ErrorCode
    status_code: int = 500
    message: str = "Internal server error"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        self.message = message or self.message


# -- auth errors ------------------------------------------------------------
class MissingTokenError(ApiError):
    code = ErrorCode.MISSING_TOKEN
    status_code = 401
    message = "Bearer token required"


class TokenInvalidError(ApiError):
    code = ErrorCode.TOKEN_INVALID
    status_code = 401
    message = "Token verification failed"


class PermissionDeniedError(ApiError):
    code = ErrorCode.PERMISSION_DENIED
    status_code = 403
    message = "Insufficient permissions"


class PhiAccessForbiddenError(ApiError):
    code = ErrorCode.PHI_ACCESS_FORBIDDEN
    status_code = 403
    message = "PHI access not permitted for this role"


class NotAssignedError(ApiError):
    code = ErrorCode.NOT_ASSIGNED
    status_code = 403
    message = "Study is assigned to another reader"


class MfaRequiredError(ApiError):
    code = ErrorCode.MFA_REQUIRED
    status_code = 403
    message = "Second-factor verification is required"


# -- domain errors ----------------------------------------------------------
class NotFoundError(ApiError):
    code = ErrorCode.NOT_FOUND
    status_code = 404
    message = "Resource not found"


class SearchFilterRequiredError(ApiError):
    code = ErrorCode.SEARCH_FILTER_REQUIRED
    status_code = 422
    message = "At least one search filter is required"


class InvalidQueryParameterError(ApiError):
    code = ErrorCode.INVALID_QUERY_PARAMETER
    status_code = 422
    message = "Query parameter is not accepted on this route"


class InstanceChunkTooLargeError(ApiError):
    code = ErrorCode.INSTANCE_CHUNK_TOO_LARGE
    status_code = 422
    message = "Instance chunk size exceeds the maximum"


class ConflictError(ApiError):
    code = ErrorCode.CONFLICT
    status_code = 409
    message = "Resource conflict"


class IngestInProgressError(ConflictError):
    code = ErrorCode.INGEST_IN_PROGRESS
    status_code = 409
    message = "An ingest job is already in progress for this study"


class FindingsPendingError(ConflictError):
    """A study cannot move to report drafting while any finding is PENDING.

    Raised by ``FindingService.assert_draftable()`` (criterion 9).  Carries the
    pending count so the 409 response can report it.
    """

    code = ErrorCode.FINDINGS_PENDING
    status_code = 409
    message = "Findings are still pending disposition"

    def __init__(self, pending_count: int, message: str | None = None) -> None:
        self.pending_count = pending_count
        super().__init__(message or f"{pending_count} finding(s) pending disposition")


class ResidencyViolationError(ApiError):
    """Pixels were processed outside the tenant's residency zone (§5.11).

    Raised by ``SegmentationDispatcher.dispatch()`` when the segmentation region
    is not in the tenant's residency zone — e.g. an ``africa`` tenant cannot
    offload to ``europe-west1`` (acceptance criterion 5).
    """

    code = ErrorCode.RESIDENCY_VIOLATION
    status_code = 403
    message = "Residency violation: data processed outside the tenant's zone"


class AuditStoreNotImmutableError(ApiError):
    code = ErrorCode.AUDIT_STORE_NOT_IMMUTABLE
    status_code = 503
    message = "Audit store does not enforce bucket lock (WORM)"


# -- research / clinical barrier (WP17) -------------------------------------
class ResearchOutputNotPermittedError(ConflictError):
    """A research-derived identifier was offered to the clinical report path.

    Raised by ``ReportService.attach()`` and ``set_section()`` when any
    identifier bears a ``RESEARCH_ID_PREFIX`` (e.g. a cohort pseudonym
    ``cs_…``).  The barrier is one-way: research output can never reach a
    signed clinical report (acceptance criterion 1).
    """

    code = ErrorCode.RESEARCH_OUTPUT_NOT_PERMITTED
    status_code = 409
    message = "Research output is not permitted in the clinical report path"


class DeidReviewPendingError(ConflictError):
    """A subject with open de-ID review items cannot be activated or measured.

    Raised by ``CohortSubjectService.activate_subject()`` and
    ``extract_features()`` while a subject has any open review item
    (acceptance criterion 3).
    """

    code = ErrorCode.DEID_REVIEW_PENDING
    status_code = 409
    message = "De-identification review is pending for this subject"

    def __init__(self, open_item_count: int, message: str | None = None) -> None:
        self.open_item_count = open_item_count
        super().__init__(message or f"{open_item_count} de-ID review item(s) pending")


# -- envelope ----------------------------------------------------------------
def _make_error_body(
    code: ErrorCode,
    message: str,
    request: Request | None = None,
) -> dict[str, Any]:
    request_id = (
        request.headers.get("X-Request-Id", uuid.uuid4().hex[:12])
        if request
        else uuid.uuid4().hex[:12]
    )
    return {"error": {"code": code.value, "message": message, "requestId": request_id}}


# -- exception handlers ------------------------------------------------------
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_make_error_body(exc.code, exc.message, request),
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        err = detail["error"]
        raw_code = err.get("code", ErrorCode.VALIDATION_ERROR.value)
        message = err.get("message", "Error")
        try:
            code = ErrorCode(raw_code)
        except ValueError:
            code = ErrorCode.VALIDATION_ERROR
        return JSONResponse(
            status_code=exc.status_code,
            content=_make_error_body(code, message, request),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=_make_error_body(ErrorCode.VALIDATION_ERROR, str(detail), request),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": ErrorCode.VALIDATION_ERROR.value,
                "message": "Request validation failed",
                "requestId": request.headers.get("X-Request-Id", uuid.uuid4().hex[:12]),
                "details": exc.errors(),
            }
        },
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_make_error_body(
            ErrorCode.VALIDATION_ERROR,
            "Internal server error",
            request,
        ),
    )


def register_handlers(app: FastAPI) -> None:
    """Register exception handlers on a FastAPI app."""
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_exception_handler)
