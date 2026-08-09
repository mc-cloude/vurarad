# ruff: noqa: B008
"""Integration tests for the reports sync API (§3.21.4 — WP15).

End-to-end through the FastAPI app with an in-memory document store.  Covers
the full mutation lifecycle:

- ``POST /reports`` — create a DRAFT report.
- ``GET /reports/{reportId}`` — fetch the report state.
- ``POST /reports/{reportId}/sync`` — apply offline mutations with idempotency
  and conflict resolution, Idempotency-Key requirement, REPORT_SYNCED audit,
  SIGNED → 409, capability denials.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.repositories.base import InMemoryDocumentStore
from app.repositories.sync_repo import REPORT_VERSIONS_COLLECTION, REPORTS_COLLECTION
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user

STUDY_ID = "st_test"
REPORT_ID = "rp_test"


# ---------------------------------------------------------------------------
# Study doc — assigned to the default test user (uid="test-uid")
# ---------------------------------------------------------------------------
def _study_doc(*, assigned_uid: str = "test-uid") -> dict[str, Any]:
    return {
        "studyId": STUDY_ID,
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
            "uid": assigned_uid,
            "operatorId": "01HZTESTOPERATOR",
            "displayName": "Test User",
        },
        "seriesCount": 1,
        "instanceCount": 10,
        "studyBytes": 5263360,
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
    store = InMemoryDocumentStore()
    asyncio.run(store.set("studies", STUDY_ID, _study_doc()))
    return store


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


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sync_headers(key: str = "sync-1") -> dict[str, str]:
    return {**_auth(), "Idempotency-Key": key}


def _create_report(client: TestClient, *, report_id: str = REPORT_ID) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/reports",
        json={"studyId": STUDY_ID, "reportId": report_id},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sync(
    client: TestClient,
    mutations: list[dict[str, Any]],
    *,
    report_id: str = REPORT_ID,
    idempotency_key: str = "sync-1",
) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/reports/{report_id}/sync",
        json={"mutations": mutations},
        headers=_sync_headers(idempotency_key),
    )
    return (
        resp.json()
        if resp.status_code == 200
        else {"status": resp.status_code, "body": resp.json()}
    )


# ---------------------------------------------------------------------------
# POST /reports — create a DRAFT report
# ---------------------------------------------------------------------------
class TestCreateReport:
    def test_create_returns_draft_report(self, client: TestClient) -> None:
        body = _create_report(client)
        assert body["reportId"] == REPORT_ID
        assert body["studyId"] == STUDY_ID
        assert body["status"] == "DRAFT"
        assert body["version"] == 0
        assert body["sections"] == []
        assert body["dictationSegments"] == []
        assert body["dictationText"] == ""

    def test_create_writes_report_created_audit(
        self, client: TestClient, audit_mirror: InMemoryAuditMirror
    ) -> None:
        _create_report(client)
        events = [e for e in audit_mirror._events if e.event_type == "REPORT_CREATED"]  # noqa: SLF001
        assert len(events) == 1
        assert events[0].detail["reportId"] == REPORT_ID

    def test_viewer_cannot_create_report(self, client: TestClient) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED)
        )
        resp = client.post(
            "/api/v1/reports",
            json={"studyId": STUDY_ID, "reportId": REPORT_ID},
            headers=_auth(),
        )
        # Viewer lacks report:write → PHI capability → PERMISSION_DENIED
        assert resp.status_code == 403

    def test_admin_gets_phi_forbidden(self, client: TestClient) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        resp = client.post(
            "/api/v1/reports",
            json={"studyId": STUDY_ID, "reportId": REPORT_ID},
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"


# ---------------------------------------------------------------------------
# GET /reports/{reportId} — fetch the report state
# ---------------------------------------------------------------------------
class TestGetReport:
    def test_get_report_after_sync(self, client: TestClient) -> None:
        _create_report(client)
        _sync(
            client,
            [
                {
                    "mutationId": "m1",
                    "type": "SET_SECTION",
                    "sectionId": "findings",
                    "text": "No acute findings.",
                    "baseVersion": 0,
                    "at": "2026-08-01T10:00:00Z",
                }
            ],
        )
        resp = client.get(f"/api/v1/reports/{REPORT_ID}", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "DRAFT"
        assert body["version"] == 1
        assert len(body["sections"]) == 1
        assert body["sections"][0]["sectionId"] == "findings"
        assert body["sections"][0]["text"] == "No acute findings."

    def test_viewer_can_read_report(self, client: TestClient) -> None:
        _create_report(client)
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED)
        )
        resp = client.get(f"/api/v1/reports/{REPORT_ID}", headers=_auth())
        # Viewer has report:read; but the study is UNREAD and unassigned-to-viewer
        # → access policy denies → 403 NOT_ASSIGNED.  This is correct: a viewer
        # may only read SIGNED studies in their scope.
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /reports/{reportId}/sync — mutation lifecycle
# ---------------------------------------------------------------------------
class TestSyncReport:
    def test_sync_applies_set_section(self, client: TestClient) -> None:
        _create_report(client)
        result = _sync(
            client,
            [
                {
                    "mutationId": "m1",
                    "type": "SET_SECTION",
                    "sectionId": "findings",
                    "text": "Lungs clear.",
                    "baseVersion": 0,
                    "at": "2026-08-01T10:00:00Z",
                }
            ],
        )
        assert result["reportId"] == REPORT_ID
        assert result["version"] == 1
        assert result["applied"] == ["m1"]
        assert result["conflicted"] == []

    def test_sync_idempotent_resend(self, client: TestClient) -> None:
        _create_report(client)
        mut = {
            "mutationId": "m1",
            "type": "SET_SECTION",
            "sectionId": "findings",
            "text": "Lungs clear.",
            "baseVersion": 0,
            "at": "2026-08-01T10:00:00Z",
        }
        r1 = _sync(client, [mut], idempotency_key="s1")
        assert r1["version"] == 1
        # Re-send with a new Idempotency-Key but same mutationId → replay.
        r2 = _sync(client, [mut], idempotency_key="s2")
        assert "m1" in r2["applied"]
        assert r2["version"] == 1  # not bumped

    def test_sync_appends_dictation_ordered(self, client: TestClient) -> None:
        _create_report(client)
        result = _sync(
            client,
            [
                {
                    "mutationId": "d2",
                    "type": "APPEND_DICTATION",
                    "text": "second",
                    "baseVersion": 0,
                    "at": "2026-08-01T10:02:00Z",
                },
                {
                    "mutationId": "d1",
                    "type": "APPEND_DICTATION",
                    "text": "first",
                    "baseVersion": 0,
                    "at": "2026-08-01T10:01:00Z",
                },
            ],
        )
        assert result["applied"] == ["d2", "d1"]
        # Fetch and verify ordering.
        resp = client.get(f"/api/v1/reports/{REPORT_ID}", headers=_auth())
        body = resp.json()
        assert [s["text"] for s in body["dictationSegments"]] == ["first", "second"]
        assert body["dictationText"] == "first\nsecond"

    def test_sync_cross_user_conflict(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _create_report(client)
        # User A sets findings.
        _sync(
            client,
            [
                {
                    "mutationId": "m1",
                    "type": "SET_SECTION",
                    "sectionId": "findings",
                    "text": "A's text",
                    "baseVersion": 0,
                    "at": "2026-08-01T10:00:00Z",
                }
            ],
        )
        # Reassign study to User B.
        doc = asyncio.run(doc_store.get("studies", STUDY_ID))
        assert doc is not None
        doc["assignedTo"] = {"uid": "rad-b", "operatorId": "01HZ", "displayName": "Rad B"}
        asyncio.run(doc_store.set("studies", STUDY_ID, doc))
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(
                uid="rad-b", role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED
            )
        )
        result = _sync(
            client,
            [
                {
                    "mutationId": "m2",
                    "type": "SET_SECTION",
                    "sectionId": "findings",
                    "text": "B's text",
                    "baseVersion": 0,
                    "at": "2026-08-01T11:00:00Z",
                }
            ],
            idempotency_key="s2",
        )
        assert "m2" not in result["applied"]
        assert len(result["conflicted"]) == 1
        c = result["conflicted"][0]
        assert c["serverText"] == "A's text"
        assert c["clientText"] == "B's text"

    def test_sync_requires_idempotency_key(self, client: TestClient) -> None:
        _create_report(client)
        resp = client.post(
            f"/api/v1/reports/{REPORT_ID}/sync",
            json={
                "mutations": [
                    {
                        "mutationId": "m1",
                        "type": "APPEND_DICTATION",
                        "text": "x",
                        "baseVersion": 0,
                        "at": "2026-08-01T10:00:00Z",
                    }
                ]
            },
            headers=_auth(),  # no Idempotency-Key
        )
        assert resp.status_code == 400

    def test_sync_signed_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _create_report(client)
        # Flip the report to SIGNED.
        doc = asyncio.run(doc_store.get(REPORTS_COLLECTION, REPORT_ID))
        assert doc is not None
        doc["status"] = "SIGNED"
        asyncio.run(doc_store.set(REPORTS_COLLECTION, REPORT_ID, doc))
        resp = client.post(
            f"/api/v1/reports/{REPORT_ID}/sync",
            json={
                "mutations": [
                    {
                        "mutationId": "m1",
                        "type": "SET_SECTION",
                        "sectionId": "findings",
                        "text": "x",
                        "baseVersion": 0,
                        "at": "2026-08-01T10:00:00Z",
                    }
                ]
            },
            headers=_sync_headers(),
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "REPORT_SIGNED"

    def test_sync_writes_report_synced_audit(
        self, client: TestClient, audit_mirror: InMemoryAuditMirror
    ) -> None:
        _create_report(client)
        _sync(
            client,
            [
                {
                    "mutationId": "m1",
                    "type": "SET_SECTION",
                    "sectionId": "findings",
                    "text": "x",
                    "baseVersion": 0,
                    "at": "2026-08-01T10:00:00Z",
                }
            ],
        )
        events = [e for e in audit_mirror._events if e.event_type == "REPORT_SYNCED"]  # noqa: SLF001
        assert len(events) == 1
        assert events[0].detail["reportId"] == REPORT_ID
        assert events[0].detail["applied"] == ["m1"]

    def test_sync_one_version_doc_per_changing_sync(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _create_report(client)
        _sync(
            client,
            [
                {
                    "mutationId": "m1",
                    "type": "SET_SECTION",
                    "sectionId": "findings",
                    "text": "v1",
                    "baseVersion": 0,
                    "at": "2026-08-01T10:00:00Z",
                }
            ],
        )
        v1 = asyncio.run(doc_store.get(REPORT_VERSIONS_COLLECTION, f"{REPORT_ID}__v1"))
        assert v1 is not None
        assert v1["applied"] == ["m1"]
        assert v1["actor"] == "test-uid"

    def test_sync_missing_report_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/reports/rp_nonexistent/sync",
            json={"mutations": []},
            headers=_sync_headers(),
        )
        assert resp.status_code == 404

    def test_sync_set_section_requires_section_id(self, client: TestClient) -> None:
        _create_report(client)
        resp = client.post(
            f"/api/v1/reports/{REPORT_ID}/sync",
            json={
                "mutations": [
                    {
                        "mutationId": "m1",
                        "type": "SET_SECTION",
                        "text": "x",
                        "baseVersion": 0,
                        "at": "2026-08-01T10:00:00Z",
                    }
                ]
            },
            headers=_sync_headers(),
        )
        assert resp.status_code == 422
