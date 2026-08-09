# ruff: noqa: B008
"""Integration tests for the report-templates API (WP9 — §3.5 templates).

Covers the catalogue listing, single-template retrieval, modality/bodyPart
filtering, the ``TEMPLATE_NOT_FOUND`` error, and the cross-role invariants:

- Templates are static, version-stamped, PHI-free content (criterion 3).
- Every permitted role (radiologist + viewer) receives a **byte-identical**
  response — there is no per-role data variation.
- Admin (no ``TEMPLATE_READ``) gets ``403 PERMISSION_DENIED`` (templates are
  non-PHI, so not ``PHI_ACCESS_FORBIDDEN``).
- A first-factor-only token gets ``403 MFA_REQUIRED``; no token → 401.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from tests.conftest import FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def templates_app() -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        users={
            "rad-token": make_user(
                uid="rad-1", role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED
            ),
            "viewer-token": make_user(
                uid="viewer-1", role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED
            ),
            "admin-token": make_user(
                uid="admin-1", role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED
            ),
            "rad-first-factor": make_user(
                uid="rad-2", role=Role.RADIOLOGIST, mfa_state=SecondFactorState.ENROLLED
            ),
        }
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    return app


@pytest.fixture
def client(templates_app: FastAPI) -> TestClient:
    return TestClient(templates_app)


def _auth(token: str = "rad-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Catalogue — GET /report-templates
# ---------------------------------------------------------------------------
class TestCatalogue:
    def test_lists_all_templates(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["schemaVersion"] == 1
        ids = sorted(t["templateId"] for t in body["templates"])
        assert ids == ["abdomen_ct", "chest_ct"]

    def test_catalogue_entries_are_versioned_metadata_only(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates", headers=_auth())
        body = r.json()
        for t in body["templates"]:
            expected = {
                "templateId",
                "modality",
                "bodyPart",
                "title",
                "version",
                "schemaVersion",
            }
            assert expected <= set(t)
            assert t["version"] >= 1
            assert t["schemaVersion"] == 1
            # Catalogue entries carry no template body.
            assert "content" not in t

    def test_filter_by_modality(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates?modality=CT", headers=_auth())
        assert len(r.json()["templates"]) == 2
        r = client.get("/api/v1/report-templates?modality=MR", headers=_auth())
        assert r.json()["templates"] == []

    def test_filter_by_body_part(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates?bodyPart=CHEST", headers=_auth())
        body = r.json()["templates"]
        assert len(body) == 1
        assert body[0]["templateId"] == "chest_ct"
        assert body[0]["bodyPart"] == "CHEST"

    def test_filter_is_case_insensitive(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates?bodyPart=chest", headers=_auth())
        assert len(r.json()["templates"]) == 1

    def test_catalogue_has_no_phi(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates", headers=_auth())
        text = r.text
        for forbidden in ("patientName", "patientBirthDate", "mrn", "patientKey"):
            assert forbidden not in text


# ---------------------------------------------------------------------------
# Retrieval — GET /report-templates/{templateId}
# ---------------------------------------------------------------------------
class TestRetrieval:
    def test_retrieve_chest_ct(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates/chest_ct", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["templateId"] == "chest_ct"
        assert body["modality"] == "CT"
        assert body["bodyPart"] == "CHEST"
        assert body["version"] == 1
        assert body["schemaVersion"] == 1
        assert "IMPRESSION" in body["content"]
        assert "FINDINGS" in body["content"]

    def test_retrieve_abdomen_ct(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates/abdomen_ct", headers=_auth())
        assert r.status_code == 200
        assert r.json()["templateId"] == "abdomen_ct"
        assert r.json()["bodyPart"] == "ABDOMEN"

    def test_unknown_template_returns_404_template_not_found(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates/does_not_exist", headers=_auth())
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"

    def test_retrieved_template_has_no_phi(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates/chest_ct", headers=_auth())
        text = r.text
        for forbidden in ("patientName", "patientBirthDate", "mrn", "patientKey"):
            assert forbidden not in text


# ---------------------------------------------------------------------------
# Cross-role invariants (criterion 3) — identical response for every role
# ---------------------------------------------------------------------------
class TestCrossRoleInvariance:
    def test_catalogue_identical_for_radiologist_and_viewer(
        self, client: TestClient
    ) -> None:
        rad = client.get("/api/v1/report-templates", headers=_auth("rad-token")).json()
        viewer = client.get(
            "/api/v1/report-templates", headers=_auth("viewer-token")
        ).json()
        assert rad == viewer

    def test_single_template_identical_for_radiologist_and_viewer(
        self, client: TestClient
    ) -> None:
        rad = client.get(
            "/api/v1/report-templates/chest_ct", headers=_auth("rad-token")
        ).json()
        viewer = client.get(
            "/api/v1/report-templates/chest_ct", headers=_auth("viewer-token")
        ).json()
        assert rad == viewer

    def test_admin_gets_permission_denied(self, client: TestClient) -> None:
        # Templates are non-PHI → admin gets PERMISSION_DENIED, not
        # PHI_ACCESS_FORBIDDEN.
        r = client.get("/api/v1/report-templates", headers=_auth("admin-token"))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_admin_denied_on_retrieval(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/report-templates/chest_ct", headers=_auth("admin-token")
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Auth / MFA enforcement
# ---------------------------------------------------------------------------
class TestAuthEnforcement:
    def test_no_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "MISSING_TOKEN"

    def test_first_factor_only_returns_mfa_required(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/report-templates", headers=_auth("rad-first-factor")
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"

    def test_retrieval_requires_auth(self, client: TestClient) -> None:
        r = client.get("/api/v1/report-templates/chest_ct")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Determinism — repeated calls are stable (version-stamped content)
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_repeated_retrieval_is_byte_identical(self, client: TestClient) -> None:
        a = client.get("/api/v1/report-templates/chest_ct", headers=_auth()).json()
        b = client.get("/api/v1/report-templates/chest_ct", headers=_auth()).json()
        assert a == b

    def test_catalogue_shape_is_stable(self, client: TestClient) -> None:
        body: Any = client.get(
            "/api/v1/report-templates", headers=_auth()
        ).json()
        assert set(body.keys()) == {"schemaVersion", "templates"}
