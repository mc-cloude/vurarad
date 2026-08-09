"""CORS preflight behaviour — correct echo, NEVER allow-credentials."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.core.errors import register_handlers
from app.core.security import SecurityHeadersMiddleware

ALLOWED_ORIGIN = "https://app.vurarad.example"
ALLOWED_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
ALLOWED_HEADERS = ["Authorization", "Content-Type", "X-Request-Id", "Idempotency-Key"]


def _cors_app(*, allow_origins: list[str]) -> FastAPI:
    app = FastAPI()
    # Mirrors the production wiring in app.main.create_app.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        allow_credentials=False,  # NEVER True — bearer auth, not cookies
        expose_headers=["X-Request-Id", "Retry-After"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    register_handlers(app)

    @app.patch("/api/v1/reports/{report_id}")
    async def update_report(report_id: str) -> dict[str, str]:
        return {"id": report_id}

    return app


def _preflight(client: TestClient, origin: str, method: str = "PATCH") -> object:
    return client.options(
        "/api/v1/reports/r-1",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )


# ---------------------------------------------------------------------------
# Preflight echoes the right headers (criterion #9)
# ---------------------------------------------------------------------------
def test_preflight_echoes_allowed_origin() -> None:
    client = TestClient(_cors_app(allow_origins=[ALLOWED_ORIGIN]))
    r = _preflight(client, ALLOWED_ORIGIN)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_preflight_echoes_allowed_methods_includes_patch() -> None:
    client = TestClient(_cors_app(allow_origins=[ALLOWED_ORIGIN]))
    r = _preflight(client, ALLOWED_ORIGIN, method="PATCH")
    allow_methods = r.headers["access-control-allow-methods"]
    assert "PATCH" in allow_methods
    for m in ("GET", "POST", "DELETE", "OPTIONS"):
        assert m in allow_methods


def test_preflight_echoes_allowed_headers() -> None:
    client = TestClient(_cors_app(allow_origins=[ALLOWED_ORIGIN]))
    r = _preflight(client, ALLOWED_ORIGIN)
    allow_headers = r.headers["access-control-allow-headers"]
    assert "authorization" in allow_headers.lower()
    assert "content-type" in allow_headers.lower()


def test_preflight_never_sets_allow_credentials() -> None:
    client = TestClient(_cors_app(allow_origins=[ALLOWED_ORIGIN]))
    r = _preflight(client, ALLOWED_ORIGIN)
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}


def test_actual_request_never_sets_allow_credentials() -> None:
    client = TestClient(_cors_app(allow_origins=[ALLOWED_ORIGIN]))
    r = client.patch(
        "/api/v1/reports/r-1",
        headers={"Origin": ALLOWED_ORIGIN},
        json={"finding": "ok"},
    )
    assert r.status_code == 200
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}
    assert r.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


# ---------------------------------------------------------------------------
# Disallowed origin
# ---------------------------------------------------------------------------
def test_preflight_disallowed_origin_not_echoed() -> None:
    client = TestClient(_cors_app(allow_origins=[ALLOWED_ORIGIN]))
    r = _preflight(client, "https://evil.example")
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_wildcard_origin_does_not_set_credentials() -> None:
    client = TestClient(_cors_app(allow_origins=["*"]))
    r = _preflight(client, "https://anywhere.example")
    # Starlette rejects preflight credentials with wildcard, but we never set
    # allow_credentials=True, so the header must never appear.
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}
