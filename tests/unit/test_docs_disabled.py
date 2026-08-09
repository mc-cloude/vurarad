"""Docs are disabled in production — /docs, /redoc, /openapi.json → 404."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.main import create_app


def _production_settings() -> Settings:
    return Settings(
        environment=Environment.production,
        gcp_project_id="vurarad-test",
        gcp_region="us-central1",
        pixel_bucket_name="pixels",
        audit_bucket_name="audit",
        firebase_project_id="vurarad-test",
    )


@pytest.fixture
def prod_app(monkeypatch: pytest.MonkeyPatch) -> object:
    """An app built with production settings (docs URLS set to None)."""
    monkeypatch.setattr("app.main.settings", _production_settings())
    return create_app()


def test_docs_return_404_in_production(prod_app: object) -> None:
    client = TestClient(prod_app)  # type: ignore[arg-type]
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_oauth_redirect_404_in_production(prod_app: object) -> None:
    client = TestClient(prod_app)  # type: ignore[arg-type]
    assert client.get("/docs/oauth2-redirect").status_code == 404


def test_docs_enabled_in_development(client: TestClient) -> None:
    # The default ``client`` fixture uses development settings.
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200
