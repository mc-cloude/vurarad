"""GET /api/v1/auth/me — valid token returns identity."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.capabilities import Role
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, make_user


def test_auth_me_with_valid_token(fake_verifier: FakeTokenVerifier, client: TestClient) -> None:
    fake_verifier.users[VALID_TOKEN] = make_user(
        uid="u-9", email="rad@example.com", role=Role.RADIOLOGIST
    )
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["uid"] == "u-9"
    assert body["role"] == "radiologist"
    assert body["mfa"]["state"] == "VERIFIED"
    assert body["mfa"]["required"] is True
    # capabilities are sorted capability strings
    assert body["capabilities"] == sorted(body["capabilities"])
    assert "report:sign" in body["capabilities"]


def test_auth_me_without_token_is_401(client: TestClient) -> None:
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"


def test_auth_me_envelope_has_request_id(client: TestClient) -> None:
    r = client.get("/api/v1/auth/me", headers={"X-Request-Id": "req-abc"})
    assert r.status_code == 401
    assert r.json()["error"]["requestId"] == "req-abc"
