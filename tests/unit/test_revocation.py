"""Token revocation — a revoked token is rejected with TOKEN_REVOKED."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, make_user

REVOKED_TOKEN = "revoked-token"


def _client(verifier: FakeTokenVerifier) -> TestClient:
    app = create_app()
    app.state.token_verifier = verifier
    return TestClient(app)


def test_revoked_token_returns_401_token_revoked() -> None:
    verifier = FakeTokenVerifier(
        default_user=make_user(),
        revoked={REVOKED_TOKEN},
    )
    client = _client(verifier)
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {REVOKED_TOKEN}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_REVOKED"


def test_non_revoked_token_still_works() -> None:
    verifier = FakeTokenVerifier(
        users={VALID_TOKEN: make_user()},
        revoked={REVOKED_TOKEN},
    )
    client = _client(verifier)
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200


def test_revocation_check_window_configured() -> None:
    from app.core.config import settings

    assert settings.token_revocation_check_seconds > 0
