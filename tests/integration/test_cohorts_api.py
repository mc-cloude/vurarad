# ruff: noqa: B008
"""Integration tests for the cohorts API (WP17 — routes 60-63, criteria 2,3,6,8).

End-to-end through the FastAPI app with an in-memory document store.  Covers
cohort CRUD, the subject-add lifecycle (through DeidPipeline), the de-ID
review-pending gate (criterion 3), versioned segmentation (criterion 6), the
``cohort:*`` capability barrier (criterion 8), and MFA enforcement.  No response
model carries ``studyId`` / ``patientKey``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.models.cohort import (
    CohortSubjectStatus,
    DeidReviewItem,
    DeidReviewStatus,
)
from app.repositories.base import InMemoryDocumentStore
from app.repositories.cohort_repo import CohortRepository
from app.repositories.deid_link_repo import DeidLinkRepository
from app.repositories.study_repo import StudyRepository
from app.services.audit_service import AuditService
from app.services.cohort_subject_service import CohortSubjectService
from app.services.deid_pipeline import DeidPipeline, DeidResult, DeidSource, StubDeidPipeline
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# Recording pipeline — proves the subject's pixels came from the DeidResult
# ---------------------------------------------------------------------------
class RecordingDeidPipeline:
    """Wraps a StubDeidPipeline and records the last DeidResult (criterion 2)."""

    def __init__(self, inner: StubDeidPipeline) -> None:
        self._inner = inner
        self.last_result: DeidResult | None = None
        self.call_count = 0

    async def deidentify(self, source: DeidSource, *, patient_key: str) -> DeidResult:
        result = await self._inner.deidentify(source, patient_key=patient_key)
        self.last_result = result
        self.call_count += 1
        return result


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------
def _study_doc(study_id: str = "st_test", patient_key: str = "pk_test") -> dict[str, Any]:
    return {
        "studyId": study_id,
        "patientKey": patient_key,
        "patientRef": "PT-2290513",
        "patientAgeSex": "41 F",
        "patientSex": "F",
        "accession": "ACC-1",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest",
        "studyDate": "2026-08-01T09:14:00Z",
        "status": "UNREAD",
        "priority": "ROUTINE",
        "seriesCount": 1,
        "instanceCount": 100,
        "studyBytes": 1000,
        "hasReport": False,
        "priorStudies": [],
        "seriesIds": [],
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_cohort(client: TestClient, name: str = "NSC-Lung") -> str:
    r = client.post(
        "/api/v1/cohorts",
        json={
            "name": name,
            "irbReference": "IRB-2026-0142",
            "irbDetermination": "EXEMPT",
        },
        headers=_auth(),
    )
    assert r.status_code == 201, r.text
    return r.json()["cohortId"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _build_app(
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
    *,
    role: Role = Role.RESEARCHER,
    mfa_state: SecondFactorState = SecondFactorState.VERIFIED,
    deid_pipeline: DeidPipeline | None = None,
    seed_study: bool = True,
) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(
            uid="researcher-1",
            role=role,
            mfa_state=mfa_state,
            operator_id="op_researcher",
            display_name="Researcher One",
        )
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    app.state.document_store = doc_store
    app.state.audit_mirror = audit_mirror
    app.state.viewer_scopes = {}
    if deid_pipeline is not None:
        app.state.deid_pipeline = deid_pipeline
    if seed_study:
        asyncio.run(doc_store.set("studies", "st_test", _study_doc()))
    return app


@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


@pytest.fixture
def audit_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def client(
    doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
) -> TestClient:
    app = _build_app(doc_store, audit_mirror)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Cohort CRUD
# ---------------------------------------------------------------------------
class TestCohortCrud:
    def test_create_cohort(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/cohorts",
            json={
                "name": "NSC-Lung",
                "irbReference": "IRB-2026-0142",
                "irbDetermination": "EXEMPT",
            },
            headers=_auth(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["cohortId"].startswith("co_")
        assert body["irbReference"] == "IRB-2026-0142"
        assert body["irbDetermination"] == "EXEMPT"
        assert body["regulatoryClass"] == "RUO"
        assert "studyId" not in body
        assert "patientKey" not in body

    def test_create_cohort_missing_irb_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/cohorts",
            json={"name": "x", "irbDetermination": "EXEMPT"},
            headers=_auth(),
        )
        assert r.status_code == 422

    def test_get_cohort(self, client: TestClient) -> None:
        cohort_id = _make_cohort(client)
        r = client.get(f"/api/v1/cohorts/{cohort_id}", headers=_auth())
        assert r.status_code == 200
        assert r.json()["cohortId"] == cohort_id

    def test_get_missing_cohort_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/cohorts/co_nope", headers=_auth())
        assert r.status_code == 404

    def test_list_cohorts(self, client: TestClient) -> None:
        _make_cohort(client, "A")
        _make_cohort(client, "B")
        r = client.get("/api/v1/cohorts", headers=_auth())
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 2


# ---------------------------------------------------------------------------
# Capability barrier (criterion 8)
# ---------------------------------------------------------------------------
class TestCapabilityBarrier:
    @pytest.fixture
    def radiologist_client(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> TestClient:
        app = _build_app(doc_store, audit_mirror, role=Role.RADIOLOGIST)
        return TestClient(app)

    @pytest.fixture
    def admin_client(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> TestClient:
        app = _build_app(doc_store, audit_mirror, role=Role.ADMIN)
        return TestClient(app)

    def test_radiologist_cannot_create_cohort(self, radiologist_client: TestClient) -> None:
        r = radiologist_client.post(
            "/api/v1/cohorts",
            json={"name": "x", "irbReference": "IRB-1", "irbDetermination": "EXEMPT"},
            headers=_auth(),
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_admin_cannot_create_cohort(self, admin_client: TestClient) -> None:
        r = admin_client.post(
            "/api/v1/cohorts",
            json={"name": "x", "irbReference": "IRB-1", "irbDetermination": "EXEMPT"},
            headers=_auth(),
        )
        assert r.status_code == 403

    def test_viewer_cannot_list_cohorts(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        app = _build_app(doc_store, audit_mirror, role=Role.VIEWER)
        c = TestClient(app)
        r = c.get("/api/v1/cohorts", headers=_auth())
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Subject add lifecycle (criterion 2 — DeidPipeline only)
# ---------------------------------------------------------------------------
class TestSubjectAddLifecycle:
    def test_add_subject_from_worklist(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        deid_link_repo = DeidLinkRepository(doc_store)
        inner = StubDeidPipeline(deid_link_repo)
        pipeline = RecordingDeidPipeline(inner)
        app = _build_app(doc_store, audit_mirror, deid_pipeline=pipeline)
        client = TestClient(app)

        cohort_id = _make_cohort(client)
        r = client.post(
            f"/api/v1/cohorts/{cohort_id}/subjects",
            json={"sourceKind": "WORKLIST", "studyId": "st_test"},
            headers=_auth(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["subjectId"].startswith("cs_")
        assert body["status"] == CohortSubjectStatus.ACTIVE.value
        assert body["pixelPassPassed"] is True
        assert "studyId" not in body
        assert "patientKey" not in body
        # The subject's pixels came from the DeidResult — no other path.
        assert pipeline.call_count == 1
        assert pipeline.last_result is not None
        assert body["deidObjectPath"] == pipeline.last_result.deid_object_path

    def test_add_subject_missing_cohort_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/cohorts/co_nope/subjects",
            json={"sourceKind": "WORKLIST", "studyId": "st_test"},
            headers=_auth(),
        )
        assert r.status_code == 404

    def test_add_subject_missing_study_404(self, client: TestClient) -> None:
        cohort_id = _make_cohort(client)
        r = client.post(
            f"/api/v1/cohorts/{cohort_id}/subjects",
            json={"sourceKind": "WORKLIST", "studyId": "st_missing"},
            headers=_auth(),
        )
        assert r.status_code == 404

    def test_add_subject_from_upload(self, client: TestClient) -> None:
        cohort_id = _make_cohort(client)
        r = client.post(
            f"/api/v1/cohorts/{cohort_id}/subjects",
            json={"sourceKind": "UPLOAD", "uploadRef": "up_123", "modality": "CT"},
            headers=_auth(),
        )
        assert r.status_code == 201, r.text
        assert r.json()["subjectId"].startswith("cs_")


# ---------------------------------------------------------------------------
# De-ID review-pending gate (criterion 3) — via the service directly
# ---------------------------------------------------------------------------
class TestReviewPendingGate:
    def _services(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> tuple[CohortSubjectService, RecordingDeidPipeline]:
        deid_link_repo = DeidLinkRepository(doc_store)
        inner = StubDeidPipeline(
            deid_link_repo,
            review_items=[DeidReviewItem(item_id="ri_1", status=DeidReviewStatus.OPEN)],
        )
        pipeline = RecordingDeidPipeline(inner)
        cohort_repo = CohortRepository(doc_store)
        study_repo = StudyRepository(doc_store)
        audit = AuditService(audit_mirror)
        subject_service = CohortSubjectService(
            cohort_repo, pipeline, doc_store, audit, study_repo=study_repo
        )
        return subject_service, pipeline

    def test_subject_with_open_items_is_pending(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        subject_service, _ = self._services(doc_store, audit_mirror)
        user = make_user(role=Role.RESEARCHER, operator_id="op_r")
        # Seed cohort + study directly.
        from app.models.cohort import Cohort

        cohort = Cohort(
            cohort_id="co_1",
            name="x",
            irb_reference="IRB-1",
            irb_determination="EXEMPT",
            created_by="op_r",
            created_at="2026-08-09T00:00:00Z",
            updated_at="2026-08-09T00:00:00Z",
        )
        asyncio.run(CohortRepository(doc_store).create_cohort(cohort))
        asyncio.run(doc_store.set("studies", "st_test", _study_doc()))

        subject = asyncio.run(
            subject_service.add_from_worklist(user, "co_1", "st_test")
        )
        assert subject.status == CohortSubjectStatus.PENDING_REVIEW

        # activate blocked (criterion 3)
        from app.core.errors import DeidReviewPendingError

        with pytest.raises(DeidReviewPendingError):
            asyncio.run(subject_service.activate_subject(user, "co_1", subject.subject_id))

        # feature extraction blocked (criterion 3)
        with pytest.raises(DeidReviewPendingError):
            asyncio.run(subject_service.extract_features(user, "co_1", subject.subject_id))

    def test_resolve_then_activate_then_extract(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        from app.models.cohort import Cohort

        subject_service, _ = self._services(doc_store, audit_mirror)
        user = make_user(role=Role.RESEARCHER, operator_id="op_r")
        cohort = Cohort(
            cohort_id="co_1",
            name="x",
            irb_reference="IRB-1",
            irb_determination="EXEMPT",
            created_by="op_r",
            created_at="2026-08-09T00:00:00Z",
            updated_at="2026-08-09T00:00:00Z",
        )
        asyncio.run(CohortRepository(doc_store).create_cohort(cohort))
        asyncio.run(doc_store.set("studies", "st_test", _study_doc()))
        subject = asyncio.run(
            subject_service.add_from_worklist(user, "co_1", "st_test")
        )
        assert subject.status == CohortSubjectStatus.PENDING_REVIEW

        asyncio.run(
            subject_service.resolve_review_item(user, "co_1", subject.subject_id, "ri_1")
        )
        activated = asyncio.run(
            subject_service.activate_subject(user, "co_1", subject.subject_id)
        )
        assert activated.status == CohortSubjectStatus.ACTIVE

        feature = asyncio.run(
            subject_service.extract_features(user, "co_1", subject.subject_id)
        )
        assert feature.feature_id.startswith("fr_")
        assert feature.subject_id == subject.subject_id


# ---------------------------------------------------------------------------
# Versioned segmentation (criterion 6)
# ---------------------------------------------------------------------------
class TestSegmentationVersioning:
    def test_masks_versioned_and_editor_recorded(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        deid_link_repo = DeidLinkRepository(doc_store)
        pipeline = RecordingDeidPipeline(StubDeidPipeline(deid_link_repo))
        app = _build_app(doc_store, audit_mirror, deid_pipeline=pipeline)
        client = TestClient(app)

        cohort_id = _make_cohort(client)
        # Add an active subject via the API.
        sr = client.post(
            f"/api/v1/cohorts/{cohort_id}/subjects",
            json={"sourceKind": "WORKLIST", "studyId": "st_test"},
            headers=_auth(),
        )
        subject_id = sr.json()["subjectId"]

        # First segmentation version.
        r1 = client.post(
            f"/api/v1/cohorts/{cohort_id}/segmentation",
            json={"subjectId": subject_id, "source": "MONAI"},
            headers=_auth(),
        )
        assert r1.status_code == 201, r1.text
        body1 = r1.json()
        assert body1["currentVersion"] == 1
        assert len(body1["versions"]) == 1
        assert body1["versions"][0]["editor"] == "op_researcher"
        assert body1["versions"][0]["maskObjectPath"].endswith("v1.nii")
        assert "studyId" not in body1
        assert "patientKey" not in body1

        # Second version — never overwrites v1.
        r2 = client.post(
            f"/api/v1/cohorts/{cohort_id}/segmentation",
            json={"subjectId": subject_id, "source": "MANUAL_EDIT"},
            headers=_auth(),
        )
        assert r2.status_code == 201, r2.text
        body2 = r2.json()
        assert body2["currentVersion"] == 2
        assert len(body2["versions"]) == 2
        assert body2["versions"][0]["maskObjectPath"].endswith("v1.nii")
        assert body2["versions"][1]["maskObjectPath"].endswith("v2.nii")
        assert body2["versions"][0]["maskObjectPath"] != body2["versions"][1]["maskObjectPath"]

    def test_segmentation_missing_subject_404(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        app = _build_app(doc_store, audit_mirror)
        client = TestClient(app)
        cohort_id = _make_cohort(client)
        r = client.post(
            f"/api/v1/cohorts/{cohort_id}/segmentation",
            json={"subjectId": "cs_nope", "source": "MONAI"},
            headers=_auth(),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Auth + MFA enforcement
# ---------------------------------------------------------------------------
class TestAuthAndMfa:
    def test_no_token_returns_401(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        app = _build_app(doc_store, audit_mirror)
        client = TestClient(app)
        r = client.get("/api/v1/cohorts")
        assert r.status_code == 401

    def test_first_factor_only_returns_403_mfa_required(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        app = _build_app(
            doc_store, audit_mirror, mfa_state=SecondFactorState.ENROLLED
        )
        client = TestClient(app)
        r = client.get("/api/v1/cohorts", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"


# ---------------------------------------------------------------------------
# DeidPipeline-only path (criterion 2 — no pixel copy without a DeidResult)
# ---------------------------------------------------------------------------
class TestDeidPipelineOnlyPath:
    """A subject is added only via DeidPipeline — no code path copies pixels
    without a DeidResult (criterion 2)."""

    _SERVICE_SRC = (
        Path(__file__).resolve().parents[2] / "app" / "services" / "cohort_subject_service.py"
    )

    def test_add_methods_route_through_deidentify(self) -> None:
        """Both add paths call ``deid.deidentify`` before materialising a subject."""
        src = self._SERVICE_SRC.read_text()
        # add_from_worklist and add_from_upload both await the pipeline.
        assert src.count("await self._deid.deidentify") >= 2
        # The subject's deidObjectPath is sourced from the DeidResult, never
        # copied from a clinical path directly.
        assert "result.deid_object_path" in src

    def test_subject_service_has_no_pixel_store_dependency(self) -> None:
        """The cohort subject service never imports the pixel object store —
        there is no code path that copies pixels."""
        src = self._SERVICE_SRC.read_text()
        assert "app.storage" not in src
        assert "ObjectStore" not in src

    def test_no_subject_created_when_deid_fails(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        """If the de-ID pipeline fails, no subject is persisted."""

        class FailingPipeline:
            async def deidentify(self, source: DeidSource, *, patient_key: str) -> DeidResult:
                raise RuntimeError("de-identification failed")

        app = _build_app(doc_store, audit_mirror, deid_pipeline=FailingPipeline())  # type: ignore[arg-type]
        client = TestClient(app, raise_server_exceptions=False)
        cohort_id = _make_cohort(client)
        r = client.post(
            f"/api/v1/cohorts/{cohort_id}/subjects",
            json={"sourceKind": "WORKLIST", "studyId": "st_test"},
            headers=_auth(),
        )
        assert r.status_code == 500
        rows = asyncio.run(doc_store.query("cohort_subjects", where=None))
        assert rows == []


# ---------------------------------------------------------------------------
# No studyId / patientKey in any cohort response (re-inforced end-to-end)
# ---------------------------------------------------------------------------
class TestNoIdentifiersInResponses:
    def test_subject_response_has_no_phi(
        self, doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
    ) -> None:
        pipeline = RecordingDeidPipeline(StubDeidPipeline(DeidLinkRepository(doc_store)))
        app = _build_app(doc_store, audit_mirror, deid_pipeline=pipeline)
        client = TestClient(app)
        cohort_id = _make_cohort(client)
        r = client.post(
            f"/api/v1/cohorts/{cohort_id}/subjects",
            json={"sourceKind": "WORKLIST", "studyId": "st_test"},
            headers=_auth(),
        )
        body = r.json()
        assert "studyId" not in body
        assert "patientKey" not in body
