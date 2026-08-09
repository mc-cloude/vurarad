"""Exhaustive ErrorCode enum, ApiError→ErrorCode resolution, uniform envelope."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.errors import (
    ApiError,
    ConflictError,
    MissingTokenError,
    NotFoundError,
    PermissionDeniedError,
    PhiAccessForbiddenError,
    TokenInvalidError,
    _make_error_body,
    register_handlers,
)
from app.models.errors import ErrorCode

ALL_API_ERRORS = [
    MissingTokenError,
    TokenInvalidError,
    PermissionDeniedError,
    PhiAccessForbiddenError,
    NotFoundError,
    ConflictError,
]


# ---------------------------------------------------------------------------
# ErrorCode enum — exhaustive
# ---------------------------------------------------------------------------
def test_error_code_enum_has_members() -> None:
    assert len(list(ErrorCode)) >= 30


def test_every_error_code_is_a_string_value() -> None:
    for code in ErrorCode:
        assert isinstance(code.value, str)
        assert code.value == code.name  # value mirrors the member name


def test_specific_codes_exist() -> None:
    for name in (
        "MISSING_TOKEN",
        "TOKEN_INVALID",
        "TOKEN_REVOKED",
        "MFA_REQUIRED",
        "MFA_ENROLMENT_REQUIRED",
        "PHI_ACCESS_FORBIDDEN",
        "AUDIT_CHAIN_BROKEN",
        "IDEMPOTENCY_MISMATCH",
    ):
        assert name in ErrorCode.__members__


# ---------------------------------------------------------------------------
# Every ApiError.code resolves to an ErrorCode member (criterion #8)
# ---------------------------------------------------------------------------
def _all_concrete_api_errors() -> list[type[ApiError]]:
    found: list[type[ApiError]] = []

    def walk(cls: type[ApiError]) -> None:
        for sub in cls.__subclasses__():
            found.append(sub)
            walk(sub)

    walk(ApiError)
    return found


def test_every_api_error_code_resolves_to_error_code_member() -> None:
    for err_cls in _all_concrete_api_errors():
        assert err_cls.code in ErrorCode, f"{err_cls.__name__}.code is not an ErrorCode member"


def test_api_error_status_codes_are_http_status() -> None:
    for err_cls in ALL_API_ERRORS:
        assert 400 <= err_cls.status_code < 600


def test_api_error_default_message_and_override() -> None:
    err = NotFoundError()
    assert err.message == "Resource not found"
    err2 = NotFoundError("missing report 42")
    assert err2.message == "missing report 42"


# ---------------------------------------------------------------------------
# Uniform envelope
# ---------------------------------------------------------------------------
def test_make_error_body_shape_with_request() -> None:
    body = _make_error_body(ErrorCode.NOT_FOUND, "nope", request=None)
    assert set(body["error"]) == {"code", "message", "requestId"}
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "nope"
    assert body["error"]["requestId"]  # non-empty


def test_make_error_body_without_request() -> None:
    body = _make_error_body(ErrorCode.CONFLICT, "c", request=None)
    assert body["error"]["requestId"]


def _error_app() -> FastAPI:
    app = FastAPI()
    register_handlers(app)

    @app.get("/raise/not-found")
    async def _nf() -> None:
        raise NotFoundError()

    @app.get("/raise/custom")
    async def _custom() -> None:
        raise NotFoundError("custom message")

    @app.get("/raise/http")
    async def _http() -> None:
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "PERMISSION_DENIED", "message": "nope"}},
        )

    @app.get("/raise/plain")
    async def _plain() -> None:
        raise RuntimeError("boom")

    @app.get("/raise/validation/{n}")
    async def _val(n: int) -> dict[str, int]:
        return {"n": n}

    return app


def test_api_error_handler_envelope() -> None:
    client = TestClient(_error_app())
    r = client.get("/raise/not-found")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert set(r.json()["error"]) == {"code", "message", "requestId"}


def test_api_error_handler_custom_message() -> None:
    client = TestClient(_error_app())
    r = client.get("/raise/custom")
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "custom message"


def test_http_exception_handler_unwraps_code() -> None:
    client = TestClient(_error_app())
    r = client.get("/raise/http")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert r.json()["error"]["message"] == "nope"


def test_generic_exception_handler_returns_500_envelope() -> None:
    client = TestClient(_error_app(), raise_server_exceptions=False)
    r = client.get("/raise/plain")
    assert r.status_code == 500
    assert "error" in r.json()
    assert set(r.json()["error"]) == {"code", "message", "requestId"}


def test_validation_error_handler_returns_422() -> None:
    client = TestClient(_error_app())
    r = client.get("/raise/validation/not-an-int")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "details" in r.json()["error"]
