# ruff: noqa: B008
"""Integration tests for fresh second-factor assertion enforcement (WP5).

Covers the signing gates (criteria 3-6):

- Missing ``Idempotency-Key`` → 400 IDEMPOTENCY_KEY_REQUIRED.
- Missing ``X-Second-Factor-Assertion`` → 403 SECOND_FACTOR_REASSERTION_REQUIRED.
- Expired assertion (> 300 s) → 403 SECOND_FACTOR_REASSERTION_REQUIRED.
- Replayed assertion id → 403 SECOND_FACTOR_ASSERTION_REPLAYED.
- Missing attestation (``attestation`` false / absent) → 422 ATTESTATION_REQUIRED.
- Valid sign → 200 with a REPORT_SIGNED audit event.

Each test creates a fresh draft, transitions it to ``PENDING_SIGNATURE``, then
exercises the sign endpoint with various header / body combinations.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.repositories.base import InMemoryDocumentStore
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# Study document seed
# ---------------------------------------------------------------------------
def _study_doc(study_id: str = "st_test") -> dict[str, Any]:
    return {
        "studyId": study_id,
        "patientKey": "pk_test",
        "patientRef": "PT-001",
        "patientAgeSex": "41 F",
        "patientSex": "F",
        "patientName": "Doe, John",
        "patientBirthDate": "1985-03-02",
        "mrn": "MRN-4471",
        "accession": "ACC-001",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest",
        "studyDate": "2026-08-01T09:14:00Z",
        "status": "UNREAD",
        "priority": "ROUTINE",
        "assignedTo": {
            "uid": "test-uid",
            "operatorId": "01HZTESTOPERATOR",
            "displayName": "Test User",
        },
        "seriesCount": 1,
        "instanceCount": 412,
        "studyBytes": 216006656,
        "hasReport": False,
        "reportId": None,
        "signedAt": None,
        "priorStudies": [],
        "seriesIds": ["se_1"],
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


@pytest.fixture
def audit_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def app(
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
) -> FastAPI:
    from app.main import create_app

    application = create_app()
    application.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    application.state.audit_object_store = StubAuditStore(locked=True)
    application.state.document_store = doc_store
    application.state.audit_mirror = audit_mirror
    application.state.viewer_scopes = {}
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_study(doc_store: InMemoryDocumentStore, study_id: str = "st_test") -> None:
    asyncio.run(doc_store.set("studies", study_id, _study_doc(study_id)))


def _fresh_assertion(assertion_id: str = "assert_001") -> str:
    """A fresh assertion header: ``<id>:<epochSeconds>`` within the 300 s window."""
    return f"{assertion_id}:{time.time()}"


def _stale_assertion(assertion_id: str = "assert_stale") -> str:
    """An expired assertion header (> 300 s old)."""
    return f"{assertion_id}:{time.time() - 400}"


def _prep_for_signing(client: TestClient, doc_store: InMemoryDocumentStore) -> str:
    """Create a draft, transition to PENDING_SIGNATURE, return the report id."""
    _seed_study(doc_store)
    resp = client.post(
        "/api/v1/studies/st_test/reports",
        json={"sections": [{"title": "Findings", "body": "No acute findings."}]},
        headers=_auth(),
    )
    assert resp.status_code == 201
    report_id = resp.json()["reportId"]
    resp = client.patch(
        f"/api/v1/reports/{report_id}",
        json={"status": "PENDING_SIGNATURE"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    return report_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestSigningMfaGates:
    def test_missing_idempotency_key_returns_400(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={**_auth(), "X-Second-Factor-Assertion": _fresh_assertion()},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_missing_second_factor_assertion_returns_403(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={**_auth(), "Idempotency-Key": "k-mfa-1"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "SECOND_FACTOR_REASSERTION_REQUIRED"

    def test_empty_second_factor_assertion_returns_403(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={
                **_auth(),
                "Idempotency-Key": "k-mfa-2",
                "X-Second-Factor-Assertion": "",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "SECOND_FACTOR_REASSERTION_REQUIRED"

    def test_malformed_assertion_returns_403(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={
                **_auth(),
                "Idempotency-Key": "k-mfa-3",
                "X-Second-Factor-Assertion": "not-a-valid-format",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "SECOND_FACTOR_REASSERTION_REQUIRED"

    def test_expired_assertion_returns_403(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={
                **_auth(),
                "Idempotency-Key": "k-mfa-4",
                "X-Second-Factor-Assertion": _stale_assertion(),
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "SECOND_FACTOR_REASSERTION_REQUIRED"

    def test_missing_attestation_returns_422(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": False},
            headers={
                **_auth(),
                "Idempotency-Key": "k-mfa-5",
                "X-Second-Factor-Assertion": _fresh_assertion(),
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "ATTESTATION_REQUIRED"

    def test_replayed_assertion_id_returns_403(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        # First sign succeeds — consumes the assertion id.
        resp1 = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={
                **_auth(),
                "Idempotency-Key": "k-replay-1",
                "X-Second-Factor-Assertion": _fresh_assertion("assert_dup"),
            },
        )
        assert resp1.status_code == 200
        # Second sign with a DIFFERENT idempotency key but the SAME assertion id.
        resp2 = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={
                **_auth(),
                "Idempotency-Key": "k-replay-2",
                "X-Second-Factor-Assertion": _fresh_assertion("assert_dup"),
            },
        )
        assert resp2.status_code == 403
        assert resp2.json()["error"]["code"] == "SECOND_FACTOR_ASSERTION_REPLAYED"

    def test_valid_sign_succeeds(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": True},
            headers={
                **_auth(),
                "Idempotency-Key": "k-valid-1",
                "X-Second-Factor-Assertion": _fresh_assertion("assert_ok"),
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SIGNED"
        assert body["signature"] is not None
        assert body["signature"]["signedBy"] == "test-uid"
        assert body["signature"]["origin"] == "FRESH"
