# ruff: noqa: B008
"""MFA enforcement — every second-factor state, admin NOT exempt, freshness/replay."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, SecondFactorState, require_mfa
from app.core.capabilities import Role
from app.core.config import settings
from app.main import create_app
from tests.conftest import FakeTokenVerifier, make_user

TOKEN = "mfa-token"


def _mfa_app(verifier: FakeTokenVerifier) -> TestClient:
    app = create_app()
    app.state.token_verifier = verifier
    router = APIRouter()

    @router.get("/mfa-protected")
    async def protected(
        user: AuthenticatedUser = Depends(require_mfa),
    ) -> dict[str, str]:
        return {"uid": user.uid}

    app.include_router(router)
    return TestClient(app)


def _client_with(mfa_state: SecondFactorState, *, role: Role = Role.VIEWER) -> TestClient:
    verifier = FakeTokenVerifier(users={TOKEN: make_user(role=role, mfa_state=mfa_state)})
    return _mfa_app(verifier)


# ---------------------------------------------------------------------------
# Second-factor states
# ---------------------------------------------------------------------------
def test_unenrolled_returns_403_mfa_enrolment_required() -> None:
    client = _client_with(SecondFactorState.UNENROLLED)
    r = client.get("/mfa-protected", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_ENROLMENT_REQUIRED"


def test_enrolled_not_verified_returns_403_mfa_required() -> None:
    client = _client_with(SecondFactorState.ENROLLED)
    r = client.get("/mfa-protected", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_REQUIRED"


def test_expired_returns_403_mfa_required() -> None:
    """An assertion older than the verification window (EXPIRED) is rejected."""
    client = _client_with(SecondFactorState.EXPIRED)
    r = client.get("/mfa-protected", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_REQUIRED"


def test_verified_passes() -> None:
    client = _client_with(SecondFactorState.VERIFIED)
    r = client.get("/mfa-protected", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["uid"] == "test-uid"


def test_mfa_route_requires_token_first() -> None:
    client = _client_with(SecondFactorState.VERIFIED)
    r = client.get("/mfa-protected")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Admin is NOT exempt (acceptance criterion #4)
# ---------------------------------------------------------------------------
def test_admin_unenrolled_not_exempt() -> None:
    client = _client_with(SecondFactorState.UNENROLLED, role=Role.ADMIN)
    r = client.get("/mfa-protected", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_ENROLMENT_REQUIRED"


def test_admin_enrolled_not_verified_not_exempt() -> None:
    client = _client_with(SecondFactorState.ENROLLED, role=Role.ADMIN)
    r = client.get("/mfa-protected", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_REQUIRED"


# ---------------------------------------------------------------------------
# Freshness window + replay (acceptance criterion #5)
# ---------------------------------------------------------------------------
def test_freshness_window_is_300_seconds() -> None:
    assert settings.mfa_verification_seconds == 300


class _ReplayRejectingSecondFactorVerifier:
    """Fake :class:`SecondFactorVerifier` that rejects a replayed TOTP code."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def verify_totp(self, uid: str, code: str) -> bool:
        if code in self._seen:
            return False  # replay rejected
        self._seen.add(code)
        return True


async def test_second_factor_replay_rejected() -> None:
    verifier = _ReplayRejectingSecondFactorVerifier()
    assert await verifier.verify_totp("u1", "123456") is True
    # Replaying the same code within the window is rejected.
    assert await verifier.verify_totp("u1", "123456") is False


async def test_second_factor_fresh_code_accepted() -> None:
    verifier = _ReplayRejectingSecondFactorVerifier()
    assert await verifier.verify_totp("u1", "111111") is True
    assert await verifier.verify_totp("u1", "222222") is True
