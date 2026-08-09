# ruff: noqa: B008
"""Integration tests for the prior-studies API (WP9 — compare-prior viewer).

Covers ``GET /studies/{studyId}/priors``:

- Priors are resolved by shared ``patientKey`` (criterion 1).
- Each prior is authorized independently via ``StudyAccessPolicy``; a prior
  assigned to a **different** radiologist is **omitted** — the request never
  403s for the whole list (criterion 1).
- Priors carry ``patientRef`` only — no patient name / DOB / MRN (criterion 2).
- A same-``patientKey`` authorization failure on the *primary* study still
  fails the whole request (403 NOT_ASSIGNED); a different-``patientKey``
  study is never returned.
- Admin → ``403 PHI_ACCESS_FORBIDDEN`` (STUDY_READ is a PHI capability).
- Viewer → only SIGNED priors within scope are included; others omitted.
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
from app.models.study import ViewerScope
from app.repositories.base import InMemoryDocumentStore
from tests.conftest import FakeTokenVerifier, StubAuditStore, make_user

RAD = "test-uid"
OTHER = "rad-other"


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------
def _study_doc(
    *,
    study_id: str,
    patient_key: str,
    patient_ref: str = "PT-1",
    status: str = "SIGNED",
    assigned_to: dict[str, Any] | None = None,
    study_date: str = "2026-06-01T09:00:00Z",
    modality: str = "CT",
    body_part: str = "CHEST",
    signed_at: str | None = "2026-06-01T10:00:00Z",
) -> dict[str, Any]:
    return {
        "studyId": study_id,
        "patientKey": patient_key,
        "patientRef": patient_ref,
        "patientAgeSex": "41 F",
        "patientSex": "F",
        "patientName": "Doe, Jane",
        "patientBirthDate": "1985-03-02",
        "mrn": "MRN-4471",
        "accession": f"ACC-{study_id}",
        "modality": modality,
        "bodyPart": body_part,
        "description": f"Study {study_id}",
        "studyDate": study_date,
        "referringPhysician": "",
        "clinicalHistory": "",
        "status": status,
        "priority": "ROUTINE",
        "assignedTo": assigned_to,
        "seriesCount": 1,
        "instanceCount": 100,
        "studyBytes": 52633600,
        "hasReport": status == "SIGNED",
        "reportId": f"rpt-{study_id}" if status == "SIGNED" else None,
        "signedAt": signed_at,
        "priorStudies": [],
        "seriesIds": [],
        "tenantId": "default",
        "createdAt": "2026-06-01T09:20:11Z",
        "updatedAt": "2026-06-01T09:20:11Z",
        "version": 1,
    }


def _assigned(uid: str) -> dict[str, Any]:
    return {"uid": uid, "operatorId": f"OP-{uid}", "displayName": uid}


def _seed_priors(doc_store: InMemoryDocumentStore) -> None:
    """Seed one current study plus three same-patient priors and an off-patient study."""
    # Current study — assigned to the calling radiologist.
    asyncio.run(
        doc_store.set(
            "studies",
            "st_current",
            _study_doc(
                study_id="st_current",
                patient_key="pk_1",
                status="IN_PROGRESS",
                assigned_to=_assigned(RAD),
                study_date="2026-07-01T09:00:00Z",
                signed_at=None,
            ),
        )
    )
    # Prior A — same patient, assigned to the calling radiologist → INCLUDED.
    asyncio.run(
        doc_store.set(
            "studies",
            "st_prior_a",
            _study_doc(
                study_id="st_prior_a",
                patient_key="pk_1",
                status="SIGNED",
                assigned_to=_assigned(RAD),
                study_date="2026-06-01T09:00:00Z",
            ),
        )
    )
    # Prior B — same patient, assigned to ANOTHER radiologist → OMITTED.
    asyncio.run(
        doc_store.set(
            "studies",
            "st_prior_b",
            _study_doc(
                study_id="st_prior_b",
                patient_key="pk_1",
                status="SIGNED",
                assigned_to=_assigned(OTHER),
                study_date="2026-05-01T09:00:00Z",
            ),
        )
    )
    # Prior C — same patient, unassigned + UNREAD (shared queue) → INCLUDED.
    asyncio.run(
        doc_store.set(
            "studies",
            "st_prior_c",
            _study_doc(
                study_id="st_prior_c",
                patient_key="pk_1",
                status="UNREAD",
                assigned_to=None,
                study_date="2026-04-01T09:00:00Z",
                signed_at=None,
            ),
        )
    )
    # Off-patient study — different patientKey → never returned.
    asyncio.run(
        doc_store.set(
            "studies",
            "st_other_patient",
            _study_doc(
                study_id="st_other_patient",
                patient_key="pk_2",
                patient_ref="PT-2",
                status="UNREAD",
                assigned_to=None,
                study_date="2026-03-01T09:00:00Z",
                signed_at=None,
            ),
        )
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


def _make_app(
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
    *,
    default_role: Role = Role.RADIOLOGIST,
    default_uid: str = RAD,
    mfa_state: SecondFactorState = SecondFactorState.VERIFIED,
    viewer_scopes: dict[str, ViewerScope] | None = None,
) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(uid=default_uid, role=default_role, mfa_state=mfa_state)
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    app.state.document_store = doc_store
    app.state.audit_mirror = audit_mirror
    app.state.viewer_scopes = viewer_scopes or {}
    return app


@pytest.fixture
def client(doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror) -> TestClient:
    _seed_priors(doc_store)
    return TestClient(_make_app(doc_store, audit_mirror))


def _auth(token: str = "valid-test-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Core: same-patientKey resolution + per-prior authorization
# ---------------------------------------------------------------------------
class TestPriorResolution:
    def test_returns_200_with_authorized_priors(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/st_current/priors", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        ids = [p["studyId"] for p in body["priors"]]
        # Prior A (assigned to self) and Prior C (unclaimed UNREAD) included.
        assert "st_prior_a" in ids
        assert "st_prior_c" in ids
        # Prior B (assigned to another radiologist) is omitted, not 403.
        assert "st_prior_b" not in ids
        # The off-patient study is never returned.
        assert "st_other_patient" not in ids

    def test_omitted_prior_does_not_403_the_request(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/st_current/priors", headers=_auth())
        assert r.status_code == 200
        assert r.json()["omittedCount"] == 1

    def test_priors_sorted_most_recent_first(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/st_current/priors", headers=_auth())
        dates = [p["studyDate"] for p in r.json()["priors"]]
        assert dates == sorted(dates, reverse=True)

    def test_priors_carry_patient_ref_only(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/st_current/priors", headers=_auth())
        text = r.text
        # No patient name / DOB / MRN anywhere in the response.
        for forbidden in ("patientName", "patientBirthDate", "mrn", "Doe, Jane"):
            assert forbidden not in text
        body = r.json()
        for p in body["priors"]:
            assert p["patientRef"] == "PT-1"
            assert "patientName" not in p
            assert "mrn" not in p

    def test_envelope_carries_study_id_and_patient_ref(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/st_current/priors", headers=_auth())
        body = r.json()
        assert body["studyId"] == "st_current"
        assert body["patientRef"] == "PT-1"
        assert set(body.keys()) == {"studyId", "patientRef", "priors", "omittedCount"}


# ---------------------------------------------------------------------------
# Primary-study authorization — can fail the whole request
# ---------------------------------------------------------------------------
class TestPrimaryAuthorization:
    def test_primary_not_found_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/does_not_exist/priors", headers=_auth())
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_primary_assigned_to_other_403_not_assigned(
        self,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_priors(doc_store)
        # Re-assign the current study to another radiologist.
        asyncio.run(
            doc_store.set(
                "studies",
                "st_current",
                _study_doc(
                    study_id="st_current",
                    patient_key="pk_1",
                    status="IN_PROGRESS",
                    assigned_to=_assigned(OTHER),
                    study_date="2026-07-01T09:00:00Z",
                    signed_at=None,
                ),
            )
        )
        c = TestClient(_make_app(doc_store, audit_mirror))
        r = c.get("/api/v1/studies/st_current/priors", headers=_auth())
        # The whole request fails — the caller may not read the primary study.
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "NOT_ASSIGNED"


# ---------------------------------------------------------------------------
# Role denials
# ---------------------------------------------------------------------------
class TestRoleDenials:
    def test_admin_gets_phi_access_forbidden(
        self,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_priors(doc_store)
        c = TestClient(
            _make_app(doc_store, audit_mirror, default_role=Role.ADMIN, default_uid="admin-1")
        )
        r = c.get("/api/v1/studies/st_current/priors", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_viewer_only_sees_signed_priors_in_scope(
        self,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_priors(doc_store)
        # Viewer scope includes prior_a (SIGNED) but not prior_c (UNREAD) and
        # not prior_b (SIGNED but out of scope).
        scopes = {
            "viewer-1": ViewerScope(
                study_ids={"st_current", "st_prior_a"},
                referring_physicians=set(),
            )
        }
        c = TestClient(
            _make_app(
                doc_store,
                audit_mirror,
                default_role=Role.VIEWER,
                default_uid="viewer-1",
                viewer_scopes=scopes,
            )
        )
        # The current study must be SIGNED and in scope for the viewer to read.
        asyncio.run(
            doc_store.set(
                "studies",
                "st_current",
                _study_doc(
                    study_id="st_current",
                    patient_key="pk_1",
                    status="SIGNED",
                    assigned_to=None,
                    study_date="2026-07-01T09:00:00Z",
                ),
            )
        )
        r = c.get("/api/v1/studies/st_current/priors", headers=_auth())
        assert r.status_code == 200, r.text
        ids = [p["studyId"] for p in r.json()["priors"]]
        assert "st_prior_a" in ids  # SIGNED + in scope
        assert "st_prior_c" not in ids  # UNREAD → omitted
        assert "st_prior_b" not in ids  # SIGNED but out of scope → omitted


# ---------------------------------------------------------------------------
# MFA enforcement
# ---------------------------------------------------------------------------
class TestMfaEnforcement:
    def test_first_factor_only_returns_mfa_required(
        self,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_priors(doc_store)
        c = TestClient(_make_app(doc_store, audit_mirror, mfa_state=SecondFactorState.ENROLLED))
        r = c.get("/api/v1/studies/st_current/priors", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"

    def test_no_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/st_current/priors")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "MISSING_TOKEN"
