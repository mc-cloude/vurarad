# ruff: noqa: B008
"""Integration tests for the reports API (WP5).

End-to-end through the FastAPI app with an in-memory document store.  Covers:

- Create draft (201), update draft (DRAFT→PENDING_SIGNATURE).
- Sign with fresh 2FA + attestation + idempotency key.
- Idempotency replay — same key returns the verbatim response with no second
  audit event.
- PATCH on SIGNED → 409 REPORT_IMMUTABLE.
- Addendum on SIGNED (parent never mutated), addendum on non-SIGNED → 409.
- Version history ascending.
- Access control: admin 403 PHI_ACCESS_FORBIDDEN, viewer 403 on mutations,
  radiologist cannot sign an unassigned study → 403 NOT_ASSIGNED.
- Transactional rollback — a flaky analytics store rolls back every write.
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
from app.repositories.report_repo import REPORTS_COLLECTION
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# Study document seed
# ---------------------------------------------------------------------------
def _study_doc(study_id: str = "st_test", assigned_uid: str = "test-uid") -> dict[str, Any]:
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
            "uid": assigned_uid,
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


def _seed_study(
    doc_store: InMemoryDocumentStore,
    study_id: str = "st_test",
    assigned_uid: str = "test-uid",
) -> None:
    asyncio.run(doc_store.set("studies", study_id, _study_doc(study_id, assigned_uid)))


def _fresh_assertion(assertion_id: str = "assert_001") -> str:
    return f"{assertion_id}:{time.time()}"


def _sign_headers(
    idem_key: str,
    assertion_id: str = "assert_001",
) -> dict[str, str]:
    return {
        **_auth(),
        "Idempotency-Key": idem_key,
        "X-Second-Factor-Assertion": _fresh_assertion(assertion_id),
    }


def _create_draft(
    client: TestClient,
    doc_store: InMemoryDocumentStore,
    *,
    study_id: str = "st_test",
    sections: list[dict[str, str]] | None = None,
) -> str:
    """Seed a study, create a draft report, return the report id."""
    _seed_study(doc_store, study_id)
    body: dict[str, Any] = {}
    if sections is not None:
        body["sections"] = sections
    else:
        body["sections"] = [{"title": "Findings", "body": "No acute findings."}]
    resp = client.post(f"/api/v1/studies/{study_id}/reports", json=body, headers=_auth())
    assert resp.status_code == 201
    return resp.json()["reportId"]


def _prep_for_signing(
    client: TestClient,
    doc_store: InMemoryDocumentStore,
    *,
    study_id: str = "st_test",
) -> str:
    """Create a draft and transition it to PENDING_SIGNATURE; return report id."""
    report_id = _create_draft(client, doc_store, study_id=study_id)
    resp = client.patch(
        f"/api/v1/reports/{report_id}",
        json={"status": "PENDING_SIGNATURE"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    return report_id


def _sign_report(
    client: TestClient,
    report_id: str,
    *,
    idem_key: str = "sign-1",
    assertion_id: str = "assert_001",
) -> Any:
    """Sign a report; return the response object."""
    return client.post(
        f"/api/v1/reports/{report_id}/sign",
        json={"attestation": True},
        headers=_sign_headers(idem_key, assertion_id),
    )


# ---------------------------------------------------------------------------
# Create draft
# ---------------------------------------------------------------------------
class TestCreateDraft:
    def test_create_draft_returns_201(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _create_draft(client, doc_store)
        resp = client.get(f"/api/v1/reports/{report_id}", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["reportId"] == report_id
        assert body["status"] == "DRAFT"
        assert body["reportType"] == "ORIGINAL"
        assert body["version"] == 1
        assert len(body["sections"]) == 1
        assert body["signature"] is None

    def test_admin_gets_phi_forbidden(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/studies/st_test/reports",
            json={"sections": []},
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_viewer_denied(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/studies/st_test/reports",
            json={"sections": []},
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Update draft + state transition + immutability
# ---------------------------------------------------------------------------
class TestUpdateDraft:
    def test_update_sections(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        report_id = _create_draft(client, doc_store)
        resp = client.patch(
            f"/api/v1/reports/{report_id}",
            json={"sections": [{"title": "Impression", "body": "Normal."}]},
            headers=_auth(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["sections"][0]["title"] == "Impression"
        assert body["status"] == "DRAFT"

    def test_transition_to_pending_signature(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _create_draft(client, doc_store)
        resp = client.patch(
            f"/api/v1/reports/{report_id}",
            json={"status": "PENDING_SIGNATURE"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "PENDING_SIGNATURE"

    def test_illegal_transition_draft_to_signed(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _create_draft(client, doc_store)
        resp = client.patch(
            f"/api/v1/reports/{report_id}",
            json={"status": "SIGNED"},
            headers=_auth(),
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_REPORT_TRANSITION"

    def test_patch_on_signed_returns_409_report_immutable(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id)
        resp = client.patch(
            f"/api/v1/reports/{report_id}",
            json={"sections": [{"title": "Findings", "body": "Changed."}]},
            headers=_auth(),
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "REPORT_IMMUTABLE"


# ---------------------------------------------------------------------------
# Sign + idempotency
# ---------------------------------------------------------------------------
class TestSign:
    def test_sign_succeeds(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = _sign_report(client, report_id)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SIGNED"
        assert body["signature"] is not None
        assert body["signature"]["signedBy"] == "test-uid"
        assert body["signature"]["origin"] == "FRESH"
        assert body["version"] == 2

    def test_sign_writes_audit_event(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id)
        events = [
            e for e in client.app.state.audit_mirror._events if e.event_type == "REPORT_SIGNED"
        ]
        assert len(events) == 1
        detail = events[0].detail
        assert detail["reportId"] == report_id
        assert detail["version"] == 2

    def test_sign_updates_study_status(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id)
        study_doc = asyncio.run(doc_store.get("studies", "st_test"))
        assert study_doc is not None
        assert study_doc["status"] == "SIGNED"
        assert study_doc["hasReport"] is True
        assert study_doc["reportId"] == report_id

    def test_idempotency_replay_returns_verbatim(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp1 = _sign_report(client, report_id, idem_key="idem-replay")
        assert resp1.status_code == 200
        body1 = resp1.json()
        # Replay with the same idempotency key and same body.
        resp2 = _sign_report(client, report_id, idem_key="idem-replay")
        assert resp2.status_code == 200
        assert resp2.json() == body1

    def test_idempotency_replay_no_duplicate_audit(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id, idem_key="idem-no-dup")
        count_before = sum(1 for e in audit_mirror._events if e.event_type == "REPORT_SIGNED")
        # Replay — should NOT write a second audit event.
        _sign_report(client, report_id, idem_key="idem-no-dup")
        count_after = sum(1 for e in audit_mirror._events if e.event_type == "REPORT_SIGNED")
        assert count_after == count_before == 1

    def test_idempotency_key_mismatch_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id, idem_key="idem-mismatch")
        # Same key, different body → 409 IDEMPOTENCY_MISMATCH.
        resp = client.post(
            f"/api/v1/reports/{report_id}/sign",
            json={"attestation": False},
            headers=_sign_headers("idem-mismatch"),
        )
        assert resp.status_code == 409

    def test_sign_already_signed_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id, idem_key="sign-first")
        # Try to sign again with a fresh key + assertion.
        resp = _sign_report(client, report_id, idem_key="sign-second", assertion_id="assert_002")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "REPORT_IMMUTABLE"


# ---------------------------------------------------------------------------
# Addendum
# ---------------------------------------------------------------------------
class TestAddendum:
    def test_addendum_on_signed_succeeds(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id)
        resp = client.post(
            f"/api/v1/reports/{report_id}/addenda",
            json={
                "sections": [{"title": "Addendum", "body": "Additional finding."}],
                "attestation": True,
            },
            headers=_sign_headers("addend-1", "assert_add"),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "SIGNED"
        assert body["reportType"] == "ADDENDUM"
        assert body["amends"] == report_id
        assert body["reportId"] != report_id
        assert body["signature"] is not None

    def test_addendum_does_not_mutate_parent(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id)
        parent_before = client.get(f"/api/v1/reports/{report_id}", headers=_auth()).json()
        # Create an addendum.
        client.post(
            f"/api/v1/reports/{report_id}/addenda",
            json={
                "sections": [{"title": "Addendum", "body": "Correction."}],
                "attestation": True,
            },
            headers=_sign_headers("addend-2", "assert_add2"),
        )
        parent_after = client.get(f"/api/v1/reports/{report_id}", headers=_auth()).json()
        assert parent_after == parent_before

    def test_addendum_on_non_signed_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _create_draft(client, doc_store)
        resp = client.post(
            f"/api/v1/reports/{report_id}/addenda",
            json={
                "sections": [{"title": "Addendum", "body": "Correction."}],
                "attestation": True,
            },
            headers=_sign_headers("addend-3", "assert_add3"),
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_REPORT_TRANSITION"


# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------
class TestVersions:
    def test_versions_ascending(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id)
        resp = client.get(f"/api/v1/reports/{report_id}/versions", headers=_auth())
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 2
        assert versions[0]["version"] == 1
        assert versions[1]["version"] == 2
        assert versions[0]["status"] == "DRAFT"
        assert versions[1]["status"] == "SIGNED"

    def test_get_specific_version(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        _sign_report(client, report_id)
        resp = client.get(f"/api/v1/reports/{report_id}/versions/2", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 2
        assert body["status"] == "SIGNED"
        assert body["signature"] is not None

    def test_get_nonexistent_version_returns_404(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        report_id = _prep_for_signing(client, doc_store)
        resp = client.get(f"/api/v1/reports/{report_id}/versions/99", headers=_auth())
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
class TestAccessControl:
    def test_admin_forbidden_on_all_report_routes(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        # (method, path, json_body_or_None)
        routes: list[tuple[str, str, dict[str, Any] | None]] = [
            ("POST", "/api/v1/studies/st_test/reports", {"sections": []}),
            ("GET", "/api/v1/reports/rp_fake", None),
            ("PATCH", "/api/v1/reports/rp_fake", {"sections": []}),
            ("POST", "/api/v1/reports/rp_fake/sign", {"attestation": True}),
            ("POST", "/api/v1/reports/rp_fake/addenda", {"sections": [], "attestation": True}),
            ("GET", "/api/v1/reports/rp_fake/versions", None),
            ("GET", "/api/v1/reports/rp_fake/versions/1", None),
        ]
        for method, path, body in routes:
            kwargs: dict[str, Any] = {"headers": _auth()}
            if body is not None:
                kwargs["json"] = body
            resp = getattr(client, method.lower())(path, **kwargs)
            assert resp.status_code == 403, f"{method} {path}: expected 403, got {resp.status_code}"
            assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_viewer_denied_on_mutations(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        # POST create — requires STUDY_WRITE.
        resp = client.post(
            "/api/v1/studies/st_test/reports",
            json={"sections": []},
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"
        # PATCH — requires REPORT_WRITE.
        resp = client.patch(
            "/api/v1/reports/rp_fake",
            json={"sections": []},
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"
        # Sign — requires REPORT_SIGN.
        resp = client.post(
            "/api/v1/reports/rp_fake/sign",
            json={"attestation": True},
            headers={
                **_auth(),
                "Idempotency-Key": "k",
                "X-Second-Factor-Assertion": _fresh_assertion(),
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_radiologist_cannot_sign_unassigned_study(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        # Create and prep as the assigned radiologist (test-uid).
        report_id = _prep_for_signing(client, doc_store)
        # Re-assign the study to a different radiologist.
        _seed_study(doc_store, assigned_uid="other-uid")
        resp = _sign_report(client, report_id, assertion_id="assert_unassigned")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "NOT_ASSIGNED"


# ---------------------------------------------------------------------------
# Transactional rollback
# ---------------------------------------------------------------------------
class _FlakyCounterStore:
    """Counter store that raises on incrementing ``reports_signed``."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    async def increment(self, counter_name: str, amount: int = 1) -> int:
        if counter_name == "reports_signed":
            raise RuntimeError("analytics store is down")
        self._counters[counter_name] = self._counters.get(counter_name, 0) + amount
        return self._counters[counter_name]

    async def read(self, counter_name: str) -> int:
        return self._counters.get(counter_name, 0)


class TestTransactionRollback:
    def test_failed_sign_rolls_back_all_writes(
        self, app: FastAPI, doc_store: InMemoryDocumentStore
    ) -> None:
        # Wire a flaky analytics store before any request.
        app.state.analytics_counter_store = _FlakyCounterStore()
        # raise_server_exceptions=False so the 500 is returned, not raised.
        client = TestClient(app, raise_server_exceptions=False)
        report_id = _prep_for_signing(client, doc_store)
        # The sign should fail because analytics.report_signed() raises.
        resp = _sign_report(client, report_id, assertion_id="assert_rollback")
        assert resp.status_code == 500
        # The report must still be PENDING_SIGNATURE — not SIGNED.
        report_doc = asyncio.run(doc_store.get(REPORTS_COLLECTION, report_id))
        assert report_doc is not None
        assert report_doc["status"] == "PENDING_SIGNATURE"
        assert report_doc.get("signature") is None
        # No version 2 should exist — list versions via the API.
        versions_resp = client.get(f"/api/v1/reports/{report_id}/versions", headers=_auth())
        assert versions_resp.status_code == 200
        version_numbers = sorted(v["version"] for v in versions_resp.json())
        assert version_numbers == [1]
        # No REPORT_SIGNED audit event.
        audit_mirror = app.state.audit_mirror
        signed_events = [e for e in audit_mirror._events if e.event_type == "REPORT_SIGNED"]
        assert len(signed_events) == 0
        # The study must not have been updated to SIGNED.
        study_doc = asyncio.run(doc_store.get("studies", "st_test"))
        assert study_doc is not None
        assert study_doc["status"] != "SIGNED"
