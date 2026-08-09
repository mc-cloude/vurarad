# ruff: noqa: B008
"""Integration tests for the findings-ingest API (§3.15.4 — acceptance criteria).

End-to-end through the FastAPI app with an in-memory document store.  Covers the
clearance-tract guarantees of ``POST /studies/{studyId}/findings/ingest``:

- **Successful ingest** (Aidoc, cleared) → CLEARED_DEVICE / PENDING findings,
  ``FINDINGS_INGESTED`` audit event, source payload retained.
- **Idempotent replay** — same ``(tenantId, studyInstanceUid, adapter, payload)``
  tuple → ``idempotent == True`` and no new findings written.
- **No-clearance vendor** (generic_v1, no registered clearance) → RUO /
  ``clinicalUseAllowed == False`` / auto-REJECTED with
  ``noClearanceReference == True``.
- **Unknown adapter** → 422 ``UNKNOWN_ADAPTER``.
- **Cross-tenant study** → 404 ``NOT_FOUND`` (never 403 — existence must not
  leak, criterion 6).
- **Admin** → 403 ``PHI_ACCESS_FORBIDDEN`` (criterion 8 — separation of duties).
- **CADt strip** — a payload carrying triage/urgency is stripped; the dropped
  count is returned in the response.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.repositories.base import InMemoryDocumentStore
from app.services.analytics_service import AnalyticsCounterStore
from app.services.finding_service import FINDINGS_COLLECTION
from app.storage.base import ObjectRef, ObjectStore
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user

STUDY_ID = "st_test"
STUDY_UID = "1.2.840.113619.2.55.3.604688119.971"
SERIES_UID = "1.2.840.113619.2.55.3.604688119.972"
SOP_UID = "1.2.3.4"


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------
class InMemorySourceStore:
    """Minimal ObjectStore that records puts for source-retention assertions."""

    def __init__(self, bucket: str = "test-source") -> None:
        self._bucket = bucket
        self._data: dict[str, bytes] = {}
        self.put_calls: list[tuple[str, bytes, str]] = []

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        self._data[key] = data
        self.put_calls.append((key, data, content_type))
        return ObjectRef(bucket=self._bucket, key=key)

    async def get_blob(self, key: str) -> bytes:
        return self._data.get(key, b"")

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return self._data.get(key, b"")[start:end]

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return [
            ObjectRef(bucket=self._bucket, key=k)
            for k in sorted(k for k in self._data if k.startswith(prefix))[:limit]
        ]

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        self._data[dst_key] = self._data.get(src_key, b"")
        return ObjectRef(bucket=self._bucket, key=dst_key)


class InMemoryCounterStore:
    """In-memory AnalyticsCounterStore for ingest telemetry."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    async def increment(self, counter_name: str, amount: int = 1) -> int:
        self.counters[counter_name] = self.counters.get(counter_name, 0) + amount
        return self.counters[counter_name]

    async def read(self, counter_name: str) -> int:
        return self.counters.get(counter_name, 0)


def _counter_store_satisfies_protocol() -> None:
    _store: AnalyticsCounterStore = InMemoryCounterStore()
    assert hasattr(_store, "increment")


def _source_store_satisfies_protocol() -> None:
    _store: ObjectStore = InMemorySourceStore()  # type: ignore[assignment]
    assert hasattr(_store, "put")


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
def _aidoc_payload(*, triage: bool = False) -> bytes:
    result: dict[str, Any] = {
        "type": "Pulmonary nodule",
        "bodyPart": "LUNG",
        "boundingBox": {"x": 10, "y": 20, "width": 100, "height": 100},
        "measurements": [{"name": "long-axis diameter", "value": 8.0, "unit": "mm"}],
        "description": "Incidental pulmonary nodule",
    }
    if triage:
        result["triage"] = "positive"
        result["urgency"] = "stat"
    return json.dumps(
        {
            "studyInstanceUid": STUDY_UID,
            "seriesInstanceUid": SERIES_UID,
            "sopInstanceUids": [SOP_UID],
            "results": [result],
        }
    ).encode()


def _generic_payload() -> bytes:
    return json.dumps(
        {
            "studyInstanceUid": STUDY_UID,
            "findings": [
                {
                    "label": "Pulmonary nodule",
                    "bodySite": "LUNG",
                    "measurements": [{"name": "long-axis diameter", "value": 8.0, "unit": "mm"}],
                    "geometry": {"bbox": [10.0, 20.0, 110.0, 120.0]},
                    "freeText": "Incidental pulmonary nodule",
                }
            ],
        }
    ).encode()


def _study_doc(study_id: str = STUDY_ID, tenant_id: str = "default") -> dict[str, Any]:
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
        "tenantId": tenant_id,
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
def source_store() -> InMemorySourceStore:
    return InMemorySourceStore()


@pytest.fixture
def counter_store() -> InMemoryCounterStore:
    return InMemoryCounterStore()


@pytest.fixture
def app(
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
    source_store: InMemorySourceStore,
    counter_store: InMemoryCounterStore,
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
    application.state.source_object_store = source_store
    application.state.counter_store = counter_store
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_study(
    doc_store: InMemoryDocumentStore, study_id: str = STUDY_ID, tenant_id: str = "default"
) -> None:
    asyncio.run(doc_store.set("studies", study_id, _study_doc(study_id, tenant_id)))


def _ingest_url(study_id: str = STUDY_ID, adapter: str = "aidoc_v1", version: str = "1") -> str:
    return (
        f"/api/v1/studies/{study_id}/findings/ingest"
        f"?adapterName={adapter}&adapterVersion={version}"
    )


# ---------------------------------------------------------------------------
# Successful ingest — cleared vendor (criterion 2)
# ---------------------------------------------------------------------------
class TestSuccessfulIngest:
    def test_cleared_vendor_produces_cleared_findings(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        source_store: InMemorySourceStore,
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(_ingest_url(), content=_aidoc_payload(), headers=_auth())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["adapterName"] == "aidoc_v1"
        assert body["vendorName"] == "Aidoc"
        assert body["studyInstanceUid"] == STUDY_UID
        assert body["idempotent"] is False
        assert body["payloadSha256"]
        assert len(body["findings"]) == 1
        f = body["findings"][0]
        assert f["regulatoryClass"] == "CLEARED_DEVICE"
        assert f["clinicalUseAllowed"] is True
        assert f["dispositionState"] == "PENDING"
        assert f["noClearanceReference"] is False
        assert body["cadtFieldsDropped"] == 0
        assert body["noClearanceReferenceCount"] == 0

        # Source payload retained.
        assert body["sourceObjectKey"] is not None
        assert body["sourceObjectKey"].startswith("findings_ingest/")
        assert len(source_store.put_calls) == 1
        key, data, content_type = source_store.put_calls[0]
        assert key.endswith("source.json")
        assert content_type == "application/json"
        assert data == _aidoc_payload()

        # Finding persisted in the findings collection.
        findings_docs = asyncio.run(doc_store.query(FINDINGS_COLLECTION))
        assert len(findings_docs) == 1

        # Audit event.
        events = [e for e in audit_mirror._events if e.event_type == "FINDINGS_INGESTED"]
        assert len(events) == 1
        detail = events[0].detail
        assert detail["studyId"] == STUDY_ID
        assert detail["adapterName"] == "aidoc_v1"
        assert detail["vendorName"] == "Aidoc"
        assert detail["findingCount"] == 1
        assert detail["operatorId"] == "01HZTESTOPERATOR"
        assert events[0].second_factor is True


# ---------------------------------------------------------------------------
# Idempotency (criterion 5)
# ---------------------------------------------------------------------------
class TestIdempotency:
    def test_replay_returns_idempotent_no_new_findings(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        source_store: InMemorySourceStore,
    ) -> None:
        _seed_study(doc_store)
        payload = _aidoc_payload()

        first = client.post(_ingest_url(), content=payload, headers=_auth())
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["idempotent"] is False
        first_finding_id = first_body["findings"][0]["findingId"]

        # Replay the exact same payload → idempotent, no new source write.
        second = client.post(_ingest_url(), content=payload, headers=_auth())
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["idempotent"] is True
        assert second_body["ingestId"] == first_body["ingestId"]
        # The original finding is returned, not a new one.
        assert second_body["findings"][0]["findingId"] == first_finding_id

        # No second source write.
        assert len(source_store.put_calls) == 1
        # Still exactly one finding.
        findings_docs = asyncio.run(doc_store.query(FINDINGS_COLLECTION))
        assert len(findings_docs) == 1

    def test_different_payload_is_not_idempotent(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
    ) -> None:
        _seed_study(doc_store)
        first = client.post(_ingest_url(), content=_aidoc_payload(), headers=_auth())
        assert first.json()["idempotent"] is False

        # A different payload (triage flag added) → new ingest, not idempotent.
        second = client.post(_ingest_url(), content=_aidoc_payload(triage=True), headers=_auth())
        assert second.status_code == 200
        assert second.json()["idempotent"] is False
        assert second.json()["ingestId"] != first.json()["ingestId"]


# ---------------------------------------------------------------------------
# No-clearance vendor → RUO / REJECTED (criterion 2)
# ---------------------------------------------------------------------------
class TestNoClearanceVendor:
    def test_generic_vendor_produces_ruo_rejected(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            _ingest_url(adapter="generic_v1"), content=_generic_payload(), headers=_auth()
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["vendorName"] == "generic"
        assert len(body["findings"]) == 1
        f = body["findings"][0]
        assert f["regulatoryClass"] == "RUO"
        assert f["clinicalUseAllowed"] is False
        assert f["dispositionState"] == "REJECTED"
        assert f["noClearanceReference"] is True
        assert body["noClearanceReferenceCount"] == 1


# ---------------------------------------------------------------------------
# Unknown adapter → 422 (criterion 1 — only pinned adapters)
# ---------------------------------------------------------------------------
class TestUnknownAdapter:
    def test_unknown_adapter_returns_422(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            _ingest_url(adapter="bogus_v1"), content=_aidoc_payload(), headers=_auth()
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "UNKNOWN_ADAPTER"

    def test_unknown_version_returns_422(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            _ingest_url(adapter="aidoc_v1", version="999"),
            content=_aidoc_payload(),
            headers=_auth(),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "UNKNOWN_ADAPTER"


# ---------------------------------------------------------------------------
# Cross-tenant → 404, never 403 (criterion 6)
# ---------------------------------------------------------------------------
class TestCrossTenant:
    def test_cross_tenant_returns_404_not_403(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        # Study belongs to "default"; user belongs to "other-tenant".
        _seed_study(doc_store, tenant_id="default")
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=replace(
                make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED),
                tenant_id="other-tenant",
            )
        )
        resp = client.post(_ingest_url(), content=_aidoc_payload(), headers=_auth())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_missing_study_returns_404(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        # No study seeded at all.
        resp = client.post(_ingest_url("st_nonexistent"), content=_aidoc_payload(), headers=_auth())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Admin → 403 PHI_ACCESS_FORBIDDEN (criterion 8)
# ---------------------------------------------------------------------------
class TestAdminForbidden:
    def test_admin_gets_phi_forbidden(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        resp = client.post(_ingest_url(), content=_aidoc_payload(), headers=_auth())
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"


# ---------------------------------------------------------------------------
# CADt strip (criterion 3)
# ---------------------------------------------------------------------------
class TestCadtStrip:
    def test_cadt_fields_dropped_count_in_response(
        self,
        client: TestClient,
        doc_store: InMemoryDocumentStore,
        counter_store: InMemoryCounterStore,
    ) -> None:
        _seed_study(doc_store)
        resp = client.post(
            _ingest_url(), content=_aidoc_payload(triage=True), headers=_auth()
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cadtFieldsDropped"] == 2  # triage + urgency
        # Still cleared + pending — CADt never affects disposition.
        f = body["findings"][0]
        assert f["regulatoryClass"] == "CLEARED_DEVICE"
        assert f["dispositionState"] == "PENDING"
        # Counter store incremented.
        assert counter_store.counters.get("findings_ingest_cadt_fields_dropped") == 2
        assert counter_store.counters.get("findings_ingest_total") == 1


# ---------------------------------------------------------------------------
# PHI redaction in free text (criterion 4) — study identifiers redacted
# ---------------------------------------------------------------------------
class TestPhiRedaction:
    def test_study_identifiers_redacted_in_stored_free_text(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_study(doc_store)
        # Payload whose free text embeds the study's patient name + MRN.
        payload = json.dumps(
            {
                "studyInstanceUid": STUDY_UID,
                "results": [
                    {
                        "type": "Pulmonary nodule",
                        "bodyPart": "LUNG",
                        "boundingBox": {"x": 10, "y": 20, "width": 100, "height": 100},
                        "description": "Nodule noted for Doe, John (MRN-4471)",
                    }
                ],
            }
        ).encode()
        resp = client.post(_ingest_url(), content=payload, headers=_auth())
        assert resp.status_code == 200, resp.text
        ingest_id = resp.json()["ingestId"]
        # The ingest record stores the redacted free text.
        record = asyncio.run(doc_store.get("findings_ingest", ingest_id))
        assert record is not None
        redacted = record["findings"][0]["redactedFreeText"]
        assert "Doe, John" not in redacted
        assert "MRN-4471" not in redacted
        assert "[REDACTED]" in redacted
