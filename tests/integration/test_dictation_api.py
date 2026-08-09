# ruff: noqa: B008
"""Integration tests for the dictation API (§3.21.3 — acceptance criterion 10).

End-to-end through the FastAPI app with an in-memory document store.  Covers:
- POST /dictation/sessions creates a session
- Admin gets 403 PHI_ACCESS_FORBIDDEN
- POST /dictation/sessions/{id}/segments appends a segment
- Idempotent on mutationId (re-send returns stored segment)
- No idempotency key → 400
- GET /dictation/sessions/{id} returns segments ordered by ``at``
- Segment text is never logged (audit events carry only ids + source)
- Segments are erased with the study (erase_for_study)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.core.errors import NotFoundError
from app.models.dictation import DictationSegmentCreate, DictationSessionCreate, DictationSource
from app.repositories.base import InMemoryDocumentStore
from app.repositories.study_repo import StudyRepository
from app.services.audit_service import AuditService
from app.services.dictation_service import DictationService
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


# ---------------------------------------------------------------------------
# POST /dictation/sessions — route 27
# ---------------------------------------------------------------------------
class TestStartSession:
    def test_creates_session(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/dictation/sessions",
            json={"studyId": "st_test", "device": "web"},
            headers={**_auth(), "Idempotency-Key": "s1"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["sessionId"].startswith("dc_")
        assert body["studyId"] == "st_test"
        assert body["uid"] == "test-uid"

    def test_admin_gets_phi_forbidden(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/dictation/sessions",
            json={"studyId": "st_test"},
            headers={**_auth(), "Idempotency-Key": "s2"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"


# ---------------------------------------------------------------------------
# POST /dictation/sessions/{id}/segments — route 28 (idempotent on mutationId)
# ---------------------------------------------------------------------------
class TestAppendSegment:
    def test_append_segment(self, client: TestClient, doc_store: InMemoryDocumentStore) -> None:
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/dictation/sessions",
            json={"studyId": "st_test"},
            headers={**_auth(), "Idempotency-Key": "s1"},
        )
        session_id = resp.json()["sessionId"]

        now = datetime.now(UTC)
        resp = client.post(
            f"/api/v1/dictation/sessions/{session_id}/segments",
            json={
                "mutationId": "mut-1",
                "at": now.isoformat(),
                "source": "SPEECH",
                "text": "Liver volume 1450 mL",
            },
            headers={**_auth(), "Idempotency-Key": "seg-1"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["mutationId"] == "mut-1"
        assert body["text"] == "Liver volume 1450 mL"

    def test_idempotent_on_mutation_id(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/dictation/sessions",
            json={"studyId": "st_test"},
            headers={**_auth(), "Idempotency-Key": "s1"},
        )
        session_id = resp.json()["sessionId"]

        now = datetime.now(UTC)
        seg_body = {
            "mutationId": "mut-1",
            "at": now.isoformat(),
            "source": "SPEECH",
            "text": "First segment",
        }
        resp1 = client.post(
            f"/api/v1/dictation/sessions/{session_id}/segments",
            json=seg_body,
            headers={**_auth(), "Idempotency-Key": "seg-1"},
        )
        assert resp1.status_code == 201
        # Re-send with same mutationId — idempotent replay
        resp2 = client.post(
            f"/api/v1/dictation/sessions/{session_id}/segments",
            json=seg_body,
            headers={**_auth(), "Idempotency-Key": "seg-2"},
        )
        assert resp2.status_code == 201
        assert resp2.json()["mutationId"] == "mut-1"
        assert resp2.json()["text"] == "First segment"

    def test_without_idempotency_key_returns_400(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/dictation/sessions",
            json={"studyId": "st_test"},
            headers={**_auth(), "Idempotency-Key": "s1"},
        )
        session_id = resp.json()["sessionId"]

        now = datetime.now(UTC)
        resp = client.post(
            f"/api/v1/dictation/sessions/{session_id}/segments",
            json={"mutationId": "mut-1", "at": now.isoformat(), "text": "test"},
            headers=_auth(),
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /dictation/sessions/{id} — route 29 (ordered by at)
# ---------------------------------------------------------------------------
class TestGetSession:
    def test_segments_ordered_by_at(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/dictation/sessions",
            json={"studyId": "st_test"},
            headers={**_auth(), "Idempotency-Key": "s1"},
        )
        session_id = resp.json()["sessionId"]

        base = datetime.now(UTC)
        for i, offset in enumerate([3, 1, 2]):
            client.post(
                f"/api/v1/dictation/sessions/{session_id}/segments",
                json={
                    "mutationId": f"mut-{offset}",
                    "at": (base + timedelta(seconds=offset)).isoformat(),
                    "source": "SPEECH",
                    "text": f"Segment {offset}",
                },
                headers={**_auth(), "Idempotency-Key": f"seg-{i}"},
            )

        resp = client.get(
            f"/api/v1/dictation/sessions/{session_id}",
            headers=_auth(),
        )
        assert resp.status_code == 200
        body = resp.json()
        segments = body["segments"]
        assert len(segments) == 3
        # Ordered by at: 1, 2, 3
        assert segments[0]["text"] == "Segment 1"
        assert segments[1]["text"] == "Segment 2"
        assert segments[2]["text"] == "Segment 3"


# ---------------------------------------------------------------------------
# Criterion 10 — segment text is redacted from logs
# ---------------------------------------------------------------------------
class TestTextRedaction:
    def test_audit_events_do_not_contain_text(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/dictation/sessions",
            json={"studyId": "st_test"},
            headers={**_auth(), "Idempotency-Key": "s1"},
        )
        session_id = resp.json()["sessionId"]

        now = datetime.now(UTC)
        client.post(
            f"/api/v1/dictation/sessions/{session_id}/segments",
            json={
                "mutationId": "mut-1",
                "at": now.isoformat(),
                "source": "SPEECH",
                "text": "SECRET PHI TEXT",
            },
            headers={**_auth(), "Idempotency-Key": "seg-1"},
        )

        for event in audit_mirror._events:
            detail_str = str(event.detail)
            assert "SECRET PHI TEXT" not in detail_str, (
                "Segment text must not appear in audit event detail"
            )
            assert "text" not in event.detail, "Audit event detail must not contain a 'text' key"


# ---------------------------------------------------------------------------
# Criterion 10 — segments erased with the study
# ---------------------------------------------------------------------------
class TestEraseForStudy:
    def test_erase_removes_sessions_and_segments(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        _seed_study(doc_store)
        svc = DictationService(doc_store, StudyRepository(doc_store), AuditService(audit_mirror))

        user = make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
        session = asyncio.run(svc.start_session(user, DictationSessionCreate(study_id="st_test")))

        now = datetime.now(UTC)
        asyncio.run(
            svc.append_segment(
                user,
                session.session_id,
                DictationSegmentCreate(
                    mutation_id="mut-1",
                    at=now,
                    source=DictationSource.SPEECH,
                    text="Text to erase",
                ),
            )
        )

        count = asyncio.run(svc.erase_for_study("st_test"))
        assert count == 2  # 1 session + 1 segment

        # Verify session is gone — _require_session raises NotFoundError after erase.
        with pytest.raises(NotFoundError):
            asyncio.run(svc._require_session(session.session_id))  # noqa: SLF001
