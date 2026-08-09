# ruff: noqa: B008
"""Integration tests for the findings API (§3.15 — acceptance criteria 8, 9).

End-to-end through the FastAPI app with an in-memory document store.  Covers:
- List findings (empty + populated)
- Admin gets 403 PHI_ACCESS_FORBIDDEN
- Disposition: confirm with text, confirm without text (422), edited without
  text (422), reject without text (OK), no idempotency key (400)
- FINDING_DISPOSITIONED audit event with findingId/priorState/newState/source/
  modelVersion/operatorId
- Viewer denied (403)
- Invalid transition (409)
- assert_draftable: pending blocks with count, all-dispositioned allows
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.core.errors import FindingsPendingError
from app.models.finding import Disposition, Finding, FindingProvenance
from app.repositories.base import InMemoryDocumentStore
from app.services.audit_service import AuditService
from app.services.finding_service import FINDINGS_COLLECTION, FindingService
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


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


def _make_finding(
    study_id: str = "st_test",
    finding_id: str = "fd_01TEST",
    state: str = "PENDING",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        study_id=study_id,
        category="ANATOMICAL_MEASUREMENT",
        label="Liver",
        measurements=[],
        provenance=FindingProvenance(
            source="VURARAD_SEGMENTATION",
            producer="TotalSegmentator",
            model_version="totalsegmentator-2.4.0",
            runtime="cpu_fast",
            produced_at=datetime.now(UTC),
        ),
        regulatory_class="MEASUREMENT",
        clinical_use_allowed=True,
        disposition=Disposition(state=state),  # type: ignore[arg-type]
    )


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


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_study(doc_store: InMemoryDocumentStore, study_id: str = "st_test") -> None:
    asyncio.run(doc_store.set("studies", study_id, _study_doc(study_id)))


def _seed_finding(
    doc_store: InMemoryDocumentStore,
    study_id: str = "st_test",
    finding_id: str = "fd_01TEST",
    state: str = "PENDING",
) -> Finding:
    f = _make_finding(study_id, finding_id, state)
    asyncio.run(doc_store.set(FINDINGS_COLLECTION, finding_id, f.model_dump()))
    return f


# ---------------------------------------------------------------------------
# GET /studies/{studyId}/findings — route 20
# ---------------------------------------------------------------------------
class TestListFindings:
    def test_empty_findings(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        _seed_study(doc_store)
        resp = client.get("/api/v1/studies/st_test/findings", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["studyId"] == "st_test"
        assert body["findings"] == []
        assert body["unavailableReasons"] == []

    def test_populated_findings(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01")
        _seed_finding(doc_store, finding_id="fd_02")
        resp = client.get("/api/v1/studies/st_test/findings", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["findings"]) == 2

    def test_admin_gets_phi_forbidden(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        resp = client.get("/api/v1/studies/st_test/findings", headers=_auth())
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"


# ---------------------------------------------------------------------------
# POST /studies/{studyId}/findings/{findingId}/disposition — route 21
# ---------------------------------------------------------------------------
class TestDisposition:
    def test_confirm_with_text(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        resp = client.post(
            "/api/v1/studies/st_test/findings/fd_01/disposition",
            json={"state": "CONFIRMED", "confirmedText": "8 mm solid nodule"},
            headers={**_auth(), "Idempotency-Key": "d1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["disposition"]["state"] == "CONFIRMED"
        assert body["disposition"]["confirmedText"] == "8 mm solid nodule"

        # Check audit event
        events = [e for e in audit_mirror._events if e.event_type == "FINDING_DISPOSITIONED"]
        assert len(events) == 1
        detail = events[0].detail
        assert detail["findingId"] == "fd_01"
        assert detail["priorState"] == "PENDING"
        assert detail["newState"] == "CONFIRMED"
        assert detail["source"] == "VURARAD_SEGMENTATION"
        assert detail["modelVersion"] == "totalsegmentator-2.4.0"
        assert detail["operatorId"] == "01HZTESTOPERATOR"

    def test_confirm_without_text_returns_422(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        resp = client.post(
            "/api/v1/studies/st_test/findings/fd_01/disposition",
            json={"state": "CONFIRMED"},
            headers={**_auth(), "Idempotency-Key": "d2"},
        )
        assert resp.status_code == 422

    def test_edited_without_text_returns_422(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/studies/st_test/findings/fd_01/disposition",
            json={"state": "EDITED"},
            headers={**_auth(), "Idempotency-Key": "d3"},
        )
        assert resp.status_code == 422

    def test_reject_without_text_ok(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        resp = client.post(
            "/api/v1/studies/st_test/findings/fd_01/disposition",
            json={"state": "REJECTED"},
            headers={**_auth(), "Idempotency-Key": "d4"},
        )
        assert resp.status_code == 200
        assert resp.json()["disposition"]["state"] == "REJECTED"

    def test_no_idempotency_key_returns_400(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        resp = client.post(
            "/api/v1/studies/st_test/findings/fd_01/disposition",
            json={"state": "REJECTED"},
            headers=_auth(),
        )
        assert resp.status_code == 400

    def test_viewer_denied(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        resp = client.post(
            "/api/v1/studies/st_test/findings/fd_01/disposition",
            json={"state": "REJECTED"},
            headers={**_auth(), "Idempotency-Key": "d5"},
        )
        # Viewer does not have study:annotate — PHI capability → PERMISSION_DENIED
        assert resp.status_code == 403

    def test_invalid_transition_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="REJECTED")
        client.post(
            "/api/v1/studies/st_test/findings/fd_01/disposition",
            json={"state": "CONFIRMED", "confirmedText": "confirmed"},
            headers={**_auth(), "Idempotency-Key": "d6"},
        )
        # REJECTED → CONFIRMED is allowed (findings can be re-confirmed).
        # Test a truly invalid one: CONFIRMED → CONFIRMED is not a valid transition
        _seed_finding(doc_store, finding_id="fd_02", state="CONFIRMED")
        resp2 = client.post(
            "/api/v1/studies/st_test/findings/fd_02/disposition",
            json={"state": "CONFIRMED", "confirmedText": "confirmed again"},
            headers={**_auth(), "Idempotency-Key": "d7"},
        )
        # CONFIRMED → CONFIRMED is not a valid transition
        assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# Criterion 9 — assert_draftable
# ---------------------------------------------------------------------------
class TestAssertDraftable:
    def test_pending_findings_block_drafting(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        _seed_finding(doc_store, finding_id="fd_02", state="PENDING")
        svc = FindingService(doc_store, AuditService(audit_mirror))
        with pytest.raises(FindingsPendingError) as exc_info:
            asyncio.run(svc.assert_draftable("st_test"))
        assert exc_info.value.pending_count == 2

    def test_all_dispositioned_allows_drafting(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        _seed_finding(doc_store, finding_id="fd_02", state="REJECTED")
        svc = FindingService(doc_store, AuditService(audit_mirror))
        # Should not raise
        asyncio.run(svc.assert_draftable("st_test"))

    def test_no_findings_allows_drafting(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        _seed_study(doc_store)
        svc = FindingService(doc_store, AuditService(audit_mirror))
        asyncio.run(svc.assert_draftable("st_test"))
