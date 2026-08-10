"""Health endpoints — no auth, no MFA, always 200."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_returns_200(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_returns_200(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["region"] == "us-central1"
    assert "environment" in body
    assert "firestore_emulator" in body


def test_healthz_no_auth_required(client: TestClient) -> None:
    # No Authorization header → still 200 (health is exempt).
    r = client.get("/healthz")
    assert r.status_code == 200


def test_readyz_no_auth_required(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
