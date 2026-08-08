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


class MfaRequiredError(ApiError):
    code = ErrorCode.MFA_REQUIRED
    status_code = 403
    message = "Second-factor verification is required"


# -- metering / ceiling / licence errors ------------------------------------
class SpendCeilingAiDisabledError(ApiError):
    code = ErrorCode.SPEND_CEILING_AI_DISABLED
    status_code = 402
    message = "AI use is disabled by the spend ceiling"


class LicenceInvalidError(ApiError):
    code = ErrorCode.LICENCE_INVALID
    status_code = 402
    message = "Licence token is invalid"


class LicenceGraceExpiredError(ApiError):
    code = ErrorCode.LICENCE_GRACE_EXPIRED
    status_code = 402
    message = "Licence grace period has expired"


# -- domain errors ----------------------------------------------------------
class NotFoundError(ApiError):
    code = ErrorCode.NOT_FOUND
    status_code = 404
    message = "Resource not found"


class ConflictError(ApiError):
    code = ErrorCode.CONFLICT
    status_code = 409
    message = "Resource conflict"


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
