# ruff: noqa: B008
"""Integration tests for the evidence API (WP13 §3.16).

End-to-end through the FastAPI app with an in-memory document store.  Covers:
- ``POST /evidence/lookup`` returns ``409 FINDING_NOT_CONFIRMED`` for a
  PENDING / REJECTED finding (criterion 1 — the first test).
- Lookup for a CONFIRMED finding returns matches and writes an
  ``EVIDENCE_LOOKUP`` audit event carrying the rule-set version (criterion 8).
- ``POST /evidence/accept`` pins an evidence reference after re-verifying the
  match, writes ``EVIDENCE_ACCEPTED``, and persists the reference on
  ``finding.evidence`` (criterion 9 — nothing accepted without an explicit
  accept; lookup alone persists nothing).
- Accept refuses a non-matching ``ruleId`` (409) and an unknown rule (404).
- Capability gates: viewer denied (403), admin PHI_ACCESS_FORBIDDEN (403).
- No background precompute of evidence: ``app.evidence`` is not imported by the
  pre-processing pipeline (criterion 2).
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.models.finding import Disposition, Finding, FindingProvenance
from app.repositories.base import InMemoryDocumentStore
from app.services.finding_service import FINDINGS_COLLECTION
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Seed helpers
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


def _make_finding(
    study_id: str = "st_test",
    finding_id: str = "fd_01TEST",
    state: str = "PENDING",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        study_id=study_id,
        category="ANATOMICAL_MEASUREMENT",
        label="Lung nodule",
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
        disposition=Disposition(  # type: ignore[arg-type]
            state=state,
            by_uid="test-uid" if state != "PENDING" else None,
            at=datetime.now(UTC) if state != "PENDING" else None,
            confirmed_text="8 mm solid nodule" if state in {"CONFIRMED", "EDITED"} else None,
        ),
    )


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
    finding = _make_finding(study_id, finding_id, state)
    asyncio.run(doc_store.set(FINDINGS_COLLECTION, finding_id, finding.model_dump()))
    return finding


_FLEISCHNER_ATTRS = {
    "noduleType": "solid",
    "diameterMm": 7,
    "patientRisk": "low",
    "noduleCount": "single",
}


# ---------------------------------------------------------------------------
# POST /evidence/lookup
# ---------------------------------------------------------------------------
class TestEvidenceLookup:
    def test_pending_finding_returns_409_finding_not_confirmed(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """Criterion 1 (first test): a PENDING finding cannot be looked up."""
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        resp = client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "FINDING_NOT_CONFIRMED"

    def test_rejected_finding_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="REJECTED")
        resp = client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "FINDING_NOT_CONFIRMED"

    def test_confirmed_finding_returns_matches_and_audits(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["findingId"] == "fd_01"
        assert body["ruleSetId"] == "fleischner-2017"
        assert body["ruleSetVersion"] == "2017.1"
        assert body["findingType"] == "pulmonary_nodule"
        assert [m["ruleId"] for m in body["matches"]] == ["solid-single-6-to-8-low-risk"]

        # EVIDENCE_LOOKUP audit event carries the rule-set version (criterion 8).
        events = [e for e in audit_mirror._events if e.event_type == "EVIDENCE_LOOKUP"]
        assert len(events) == 1
        detail = events[0].detail
        assert detail["ruleSetId"] == "fleischner-2017"
        assert detail["ruleSetVersion"] == "2017.1"
        assert detail["findingId"] == "fd_01"
        assert detail["matchCount"] == 1

    def test_lookup_does_not_persist_evidence_refs(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """Criterion 9: lookup alone must not pin evidence to the finding."""
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        doc = asyncio.run(doc_store.get(FINDINGS_COLLECTION, "fd_01"))
        assert doc is not None
        assert doc["evidence"] == []

    def test_unknown_rule_set_returns_404(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_01",
                "ruleSetId": "no-such-ruleset",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_unknown_finding_returns_404(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_missing",
                "ruleSetId": "fleischner-2017",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_viewer_denied(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        # Viewer lacks evidence:read (a PHI capability) → PERMISSION_DENIED.
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_admin_gets_phi_forbidden(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/lookup",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"


# ---------------------------------------------------------------------------
# POST /evidence/accept
# ---------------------------------------------------------------------------
class TestEvidenceAccept:
    def test_accept_persists_evidence_ref_and_audits(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/accept",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "ruleId": "solid-single-6-to-8-low-risk",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers={**_auth(), "Idempotency-Key": "e1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["evidence"]) == 1
        ref = body["evidence"][0]
        assert ref["ruleId"] == "solid-single-6-to-8-low-risk"
        assert ref["evidenceId"].startswith("ev_")
        assert ref["citationId"]  # backed by the rule-set citation

        # Persisted to the store.
        doc = asyncio.run(doc_store.get(FINDINGS_COLLECTION, "fd_01"))
        assert doc is not None and len(doc["evidence"]) == 1

        # EVIDENCE_ACCEPTED audit event carries the rule-set version (criterion 8).
        events = [e for e in audit_mirror._events if e.event_type == "EVIDENCE_ACCEPTED"]
        assert len(events) == 1
        detail = events[0].detail
        assert detail["ruleSetId"] == "fleischner-2017"
        assert detail["ruleSetVersion"] == "2017.1"
        assert detail["ruleId"] == "solid-single-6-to-8-low-risk"
        assert detail["evidenceId"] == ref["evidenceId"]

    def test_accept_non_matching_rule_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        # The attributes match the 6-8 low rule, not the 8+ rule.
        resp = client.post(
            "/api/v1/evidence/accept",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "ruleId": "solid-single-8-or-more",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers={**_auth(), "Idempotency-Key": "e2"},
        )
        assert resp.status_code == 409

    def test_accept_unknown_rule_returns_404(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/accept",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "ruleId": "no-such-rule",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers={**_auth(), "Idempotency-Key": "e3"},
        )
        assert resp.status_code == 404

    def test_accept_without_idempotency_key_returns_400(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/accept",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "ruleId": "solid-single-6-to-8-low-risk",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers=_auth(),
        )
        assert resp.status_code == 400

    def test_accept_pending_finding_returns_409(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="PENDING")
        resp = client.post(
            "/api/v1/evidence/accept",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "ruleId": "solid-single-6-to-8-low-risk",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers={**_auth(), "Idempotency-Key": "e4"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "FINDING_NOT_CONFIRMED"

    def test_accept_viewer_denied(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED)
        )
        _seed_study(doc_store)
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/accept",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "ruleId": "solid-single-6-to-8-low-risk",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers={**_auth(), "Idempotency-Key": "e5"},
        )
        assert resp.status_code == 403

    def test_accept_for_unassigned_study_denied(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        # A study assigned to a different radiologist: write is not allowed.
        study = _study_doc()
        study["assignedTo"] = {
            "uid": "other-radiologist",
            "operatorId": "OTHEROP",
            "displayName": "Other",
        }
        asyncio.run(doc_store.set("studies", "st_test", study))
        _seed_finding(doc_store, finding_id="fd_01", state="CONFIRMED")
        resp = client.post(
            "/api/v1/evidence/accept",
            json={
                "findingId": "fd_01",
                "ruleSetId": "fleischner-2017",
                "ruleId": "solid-single-6-to-8-low-risk",
                "attributes": _FLEISCHNER_ATTRS,
            },
            headers={**_auth(), "Idempotency-Key": "e6"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "NOT_ASSIGNED"


# ---------------------------------------------------------------------------
# Criterion 2 — no background precompute of evidence
# ---------------------------------------------------------------------------
class TestNoPrecomputeOfEvidence:
    def test_preprocessing_does_not_import_evidence(self) -> None:
        """``app.evidence`` must not be imported by the pre-processing pipeline."""
        preprocessing_dir = REPO_ROOT / "app" / "services" / "preprocessing"
        offenders: list[str] = []
        for path in sorted(preprocessing_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "app.evidence" or alias.name.startswith("app.evidence."):
                            offenders.append(f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == "app.evidence" or module.startswith("app.evidence."):
                        offenders.append(f"{path.name}: from {module} import ...")
        assert offenders == [], f"preprocessing imports evidence: {offenders}"
