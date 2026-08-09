# ruff: noqa: B008
"""Integration tests for patient erasure (WP7).

The legacy erasure returned ``500``; this suite asserts it returns ``200`` and
actually deletes patient/studies/series/reports+versions/GCS objects/worklist
rows while retaining every audit record (and appending ``PATIENT_ERASED``).
Also covers the confirmation mismatch, patient-not-found, idempotency, and
fresh-2FA (300s rule) requirements.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.models.audit import AuditEvent
from app.repositories.base import InMemoryDocumentStore
from app.storage.base import ObjectRef
from tests.conftest import make_user


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class RecordingPixelStore:
    """Object store that records deletes and serves a seeded key set."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self._objects: dict[str, bytes] = dict(objects or {})
        self.deleted_keys: list[str] = []

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return [
            ObjectRef(bucket="pix", key=key)
            for key in self._objects
            if key.startswith(prefix)
        ][:limit]

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)
        self.deleted_keys.append(key)


class FakeSecondFactorVerifier:
    def __init__(self, valid_code: str = "123456") -> None:
        self.valid_code = valid_code
        self.calls: list[tuple[str, str]] = []

    async def verify_totp(self, uid: str, code: str) -> bool:
        self.calls.append((uid, code))
        return code == self.valid_code


class StubTokenVerifier:
    def __init__(self, users: dict[str, Any]) -> None:
        self.users = users

    async def verify(self, id_token: str) -> Any:
        user = self.users.get(id_token)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "TOKEN_INVALID", "message": "Unknown token"}},
            )
        return user


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------
def _seed_patient(store: InMemoryDocumentStore, key: str, ref: str) -> None:
    import asyncio

    asyncio.run(
        store.set(
            "patients",
            key,
            {"patientRef": ref, "patientName": "Doe, John", "mrn": "MRN-1", "studyIds": []},
        )
    )


def _seed_doc_store() -> InMemoryDocumentStore:
    import asyncio

    store = InMemoryDocumentStore()
    # Patient pk-1 (full), patient pk-2 (for mismatch test).
    asyncio.run(
        store.set("patients", "pk-1", {"patientRef": "PT-CONFIRM", "studyIds": ["st-1", "st-2"]})
    )
    asyncio.run(store.set("patients", "pk-2", {"patientRef": "PT-2", "studyIds": []}))

    # Studies for pk-1.
    asyncio.run(store.set("studies", "st-1", {"patientKey": "pk-1", "studyId": "st-1"}))
    asyncio.run(store.set("studies", "st-2", {"patientKey": "pk-1", "studyId": "st-2"}))
    # An unrelated study (other patient) must survive.
    asyncio.run(store.set("studies", "st-other", {"patientKey": "pk-other", "studyId": "st-other"}))

    # Series for each study.
    asyncio.run(store.set("series", "se-1", {"study_id": "st-1", "series_id": "se-1"}))
    asyncio.run(store.set("series", "se-2", {"study_id": "st-2", "series_id": "se-2"}))

    # Reports + versions keyed by patientKey.
    asyncio.run(store.set("reports", "r-1", {"patientKey": "pk-1"}))
    asyncio.run(store.set("reports", "r-2", {"patientKey": "pk-1"}))
    asyncio.run(store.set("report_versions", "rv-1", {"patientKey": "pk-1"}))

    # Worklist index with rows for st-1, st-2 and an unrelated study.
    asyncio.run(
        store.set(
            "worklist_index",
            "current",
            {
                "items": [
                    {"studyId": "st-1", "patientKey": "pk-1"},
                    {"studyId": "st-2", "patientKey": "pk-1"},
                    {"studyId": "st-other", "patientKey": "pk-other"},
                ],
                "count": 3,
                "totalKnown": 3,
            },
        )
    )
    return store


def _seed_pixel_store() -> RecordingPixelStore:
    return RecordingPixelStore(
        {
            "studies/st-1/se-1/0000.dcm": b"dcm1",
            "studies/st-1/se-1/0001.dcm": b"dcm1b",
            "studies/st-2/se-2/0000.dcm": b"dcm2",
            "studies/st-other/se-x/0000.dcm": b"keep",
        }
    )


def _seed_audit_mirror() -> InMemoryAuditMirror:
    mirror = InMemoryAuditMirror()
    # Seed three pre-existing audit events (must be retained by erasure).
    prev = AuditEvent.genesis().hash
    for seq, etype in enumerate(("STUDY_ACCESSED", "REPORT_SIGNED", "USER_LOGIN"), start=1):
        event = AuditEvent(
            seq=seq, prev_hash=prev, event_type=etype, actor="rad-1", second_factor=True
        )
        event.seal()
        mirror._events.append(event)
        prev = event.hash
    return mirror


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def erasure_app() -> FastAPI:
    from app.main import create_app

    doc_store = _seed_doc_store()
    pixel_store = _seed_pixel_store()
    audit_mirror = _seed_audit_mirror()
    verifier = StubTokenVerifier(
        users={
            "admin-token": make_user(
                uid="admin-uid", role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED
            ),
            "rad-token": make_user(
                uid="rad-uid", role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED
            ),
        }
    )
    app = create_app()
    app.state.token_verifier = verifier
    app.state.document_store = doc_store
    app.state.object_store = pixel_store
    app.state.audit_mirror = audit_mirror
    app.state.second_factor_verifier = FakeSecondFactorVerifier()
    return app


@pytest.fixture
def client(erasure_app: FastAPI) -> TestClient:
    return TestClient(erasure_app)


@pytest.fixture
def doc_store(erasure_app: FastAPI) -> InMemoryDocumentStore:
    return erasure_app.state.document_store  # type: ignore[return-value]


@pytest.fixture
def pixel_store(erasure_app: FastAPI) -> RecordingPixelStore:
    return erasure_app.state.object_store  # type: ignore[return-value]


@pytest.fixture
def audit_mirror(erasure_app: FastAPI) -> InMemoryAuditMirror:
    return erasure_app.state.audit_mirror  # type: ignore[return-value]


def _auth(token: str = "admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-MFA-Code": "123456"}


# ---------------------------------------------------------------------------
# 1. Erasure completeness — returns 200 and actually works
# ---------------------------------------------------------------------------
class TestErasureCompleteness:
    def test_erasure_returns_200_with_tally(self, client: TestClient) -> None:
        r = client.request(
            "DELETE",
            "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"},
            headers=_auth(),
        )
        assert r.status_code == 200, r.text
        deleted = r.json()["deleted"]
        assert deleted["patients"] == 1
        assert deleted["studies"] == 2
        assert deleted["series"] == 2
        assert deleted["reports"] == 2
        assert deleted["report_versions"] == 1
        assert deleted["gcs_objects"] == 3
        assert deleted["worklist_index"] == 2

    def test_patient_doc_deleted(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        import asyncio

        assert asyncio.run(doc_store.get("patients", "pk-1")) is None

    def test_studies_and_series_deleted(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        import asyncio

        client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        assert asyncio.run(doc_store.get("studies", "st-1")) is None
        assert asyncio.run(doc_store.get("studies", "st-2")) is None
        assert asyncio.run(doc_store.get("series", "se-1")) is None
        assert asyncio.run(doc_store.get("series", "se-2")) is None
        # Unrelated study survives.
        assert asyncio.run(doc_store.get("studies", "st-other")) is not None

    def test_reports_and_versions_deleted(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        import asyncio

        client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        assert asyncio.run(doc_store.get("reports", "r-1")) is None
        assert asyncio.run(doc_store.get("reports", "r-2")) is None
        assert asyncio.run(doc_store.get("report_versions", "rv-1")) is None

    def test_gcs_objects_deleted(
        self, client: TestClient, pixel_store: RecordingPixelStore
    ) -> None:
        client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        deleted = set(pixel_store.deleted_keys)
        assert "studies/st-1/se-1/0000.dcm" in deleted
        assert "studies/st-1/se-1/0001.dcm" in deleted
        assert "studies/st-2/se-2/0000.dcm" in deleted
        # Unrelated object survives.
        assert "studies/st-other/se-x/0000.dcm" not in deleted

    def test_worklist_rows_removed(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        import asyncio

        client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        wl = asyncio.run(doc_store.get("worklist_index", "current"))
        assert wl is not None
        study_ids = {item["studyId"] for item in wl["items"]}
        assert "st-1" not in study_ids
        assert "st-2" not in study_ids
        assert "st-other" in study_ids


# ---------------------------------------------------------------------------
# 2. Audit retention + PATIENT_ERASED record
# ---------------------------------------------------------------------------
class TestErasureAudit:
    def test_audit_records_retained_and_patient_erased_record_added(
        self, client: TestClient, audit_mirror: InMemoryAuditMirror
    ) -> None:
        before = len(audit_mirror._events)
        client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        after = len(audit_mirror._events)
        # Pre-existing records retained (none deleted) + one PATIENT_ERASED added.
        assert after == before + 1
        erased = [e for e in audit_mirror._events if e.event_type == "PATIENT_ERASED"]
        assert len(erased) == 1
        assert erased[0].patient_key == "pk-1"
        # The three pre-existing events are still present.
        types = {e.event_type for e in audit_mirror._events}
        assert {"STUDY_ACCESSED", "REPORT_SIGNED", "USER_LOGIN"} <= types


# ---------------------------------------------------------------------------
# 3. Confirmation mismatch → 422
# ---------------------------------------------------------------------------
class TestErasureConfirmation:
    def test_mismatch_returns_422(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        import asyncio

        r = client.request(
            "DELETE", "/api/v1/admin/patients/pk-2",
            json={"confirmPatientRef": "WRONG"}, headers=_auth(),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "ERASURE_CONFIRMATION_MISMATCH"
        # The patient must NOT have been deleted.
        assert asyncio.run(doc_store.get("patients", "pk-2")) is not None


# ---------------------------------------------------------------------------
# 4. Patient not found → 404
# ---------------------------------------------------------------------------
class TestErasureNotFound:
    def test_unknown_patient_returns_404(self, client: TestClient) -> None:
        r = client.request(
            "DELETE", "/api/v1/admin/patients/pk-nonexistent",
            json={"confirmPatientRef": "x"}, headers=_auth(),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "PATIENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# 5. Idempotency — second call returns 200 with zero counts
# ---------------------------------------------------------------------------
class TestErasureIdempotency:
    def test_second_call_returns_zero_counts(self, client: TestClient) -> None:
        r1 = client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        assert r1.status_code == 200
        r2 = client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        assert r2.status_code == 200
        assert all(count == 0 for count in r2.json()["deleted"].values())


# ---------------------------------------------------------------------------
# 6. Fresh 2FA (300s rule)
# ---------------------------------------------------------------------------
class TestErasureFreshMfa:
    def test_missing_mfa_code_returns_403(self, client: TestClient) -> None:
        r = client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"},
            headers={"Authorization": "Bearer admin-token"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"

    def test_invalid_mfa_code_returns_403(self, client: TestClient) -> None:
        r = client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"},
            headers={"Authorization": "Bearer admin-token", "X-MFA-Code": "bad"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_CHALLENGE_FAILED"


# ---------------------------------------------------------------------------
# 7. Capability denial — radiologist → 403
# ---------------------------------------------------------------------------
class TestErasureDenial:
    def test_radiologist_forbidden(self, client: TestClient) -> None:
        r = client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"},
            headers={"Authorization": "Bearer rad-token", "X-MFA-Code": "123456"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 8. No MRN in any request line — opaque patientKey only (§3.12 [#14])
# ---------------------------------------------------------------------------
class TestErasureNoMrnInRequestLine:
    """The erasure path parameter is an opaque ``patientKey``, never an MRN.
    No request line in this package may carry an MRN value."""

    MRN = "MRN-1"  # the MRN seeded into the patient document

    def test_erasure_route_path_template_has_no_mrn(self, erasure_app: FastAPI) -> None:
        for route in erasure_app.routes:
            path = getattr(route, "path", "")
            if "/admin/patients/" in path and "{" in path:
                assert "mrn" not in path.lower(), f"route path leaks MRN: {path}"

    def test_request_line_never_contains_mrn(self, client: TestClient) -> None:
        r = client.request(
            "DELETE", "/api/v1/admin/patients/pk-1",
            json={"confirmPatientRef": "PT-CONFIRM"}, headers=_auth(),
        )
        assert r.status_code == 200
        request_line = f"{r.request.method} {r.request.url.path}"
        assert self.MRN not in request_line
        assert "mrn" not in request_line.lower()
        # The opaque key — not the MRN — is what appears in the request line.
        assert "pk-1" in request_line
