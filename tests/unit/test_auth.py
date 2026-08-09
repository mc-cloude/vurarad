# ruff: noqa: B008
"""Token extraction, 401-without-header, 403-unknown-role, capability gates."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, SecondFactorState, require_capability
from app.core.capabilities import Capability, Role
from app.main import create_app
from tests.conftest import (
    VALID_TOKEN,
    FakeTokenVerifier,
    make_user,
    make_user_with_unknown_role,
)


def _build_app(verifier: FakeTokenVerifier, *, with_capability_route: bool = False) -> TestClient:
    app = create_app()
    app.state.token_verifier = verifier
    if with_capability_route:
        cap_router = APIRouter()

        @cap_router.get("/cap/study-read")
        async def needs_study_read(
            user: AuthenticatedUser = Depends(require_capability(Capability.STUDY_READ)),
        ) -> dict[str, str]:
            return {"uid": user.uid}

        app.include_router(cap_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 401 without / malformed Authorization header
# ---------------------------------------------------------------------------
def test_no_auth_header_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"


def test_empty_auth_header_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/auth/me", headers={"Authorization": ""})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"


def test_wrong_scheme_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Token abc"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"


def test_bearer_without_token_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"


# ---------------------------------------------------------------------------
# Valid token → user identity
# ---------------------------------------------------------------------------
def test_valid_token_returns_user(fake_verifier: FakeTokenVerifier, client: TestClient) -> None:
    fake_verifier.users[VALID_TOKEN] = make_user(
        uid="u-123", email="rad@example.com", role=Role.RADIOLOGIST
    )
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["uid"] == "u-123"
    assert body["email"] == "rad@example.com"
    assert body["role"] == "radiologist"
    assert "report:write" in body["capabilities"]
    assert body["mfa"]["state"] == "VERIFIED"


# ---------------------------------------------------------------------------
# is_mfa_verified property
# ---------------------------------------------------------------------------
def test_is_mfa_verified_true_when_verified() -> None:
    user = make_user(mfa_state=SecondFactorState.VERIFIED)
    assert user.is_mfa_verified is True


def test_is_mfa_verified_false_when_not_verified() -> None:
    user = make_user(mfa_state=SecondFactorState.UNENROLLED)
    assert user.is_mfa_verified is False


# ---------------------------------------------------------------------------
# Verifier failures
# ---------------------------------------------------------------------------
def test_verifier_generic_exception_returns_401_token_invalid() -> None:
    verifier = FakeTokenVerifier(exc=RuntimeError("signature mismatch"))
    client = _build_app(verifier)
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_INVALID"


def test_unknown_role_returns_403_forbidden() -> None:
    verifier = FakeTokenVerifier(default_user=make_user_with_unknown_role("ghost"))
    client = _build_app(verifier)
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer any"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# require_capability gate
# ---------------------------------------------------------------------------
def test_require_capability_allows_when_present() -> None:
    verifier = FakeTokenVerifier(default_user=make_user(role=Role.RADIOLOGIST))
    client = _build_app(verifier, with_capability_route=True)
    r = client.get("/cap/study-read", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json()["uid"] == "test-uid"


def test_require_capability_denies_when_absent() -> None:
    # ADMIN has zero clinical capabilities (separation of duties).
    verifier = FakeTokenVerifier(default_user=make_user(role=Role.ADMIN))
    client = _build_app(verifier, with_capability_route=True)
    r = client.get("/cap/study-read", headers={"Authorization": "Bearer t"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Every non-health route requires auth (no header → 401)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/v1/auth/me"])
def test_protected_routes_require_auth(path: str, client: TestClient) -> None:
    assert client.get(path).status_code == 401
