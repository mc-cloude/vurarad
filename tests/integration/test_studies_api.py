# ruff: noqa: B008
"""Integration tests for the studies API (§3.3–3.6 acceptance criteria).

End-to-end through the FastAPI app with an instrumented document store that
counts Firestore reads.  Covers worklist read-count, envelope shape, search
filter requirements, series read-count, access-URL chunking, capability
denials (admin/viewer/radiologist), PHI separation, and MFA enforcement.
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
from app.models.series import Instance, Series, StackOrderBasis, StackOrderConfidence
from app.models.study import (
    AccessUrlChunk,
    AccessUrlEntry,
    InstanceGeometry,
    PatientIdentity,
    SearchResult,
    SeriesListResponse,
    SeriesSummary,
    StudyDetail,
    ViewerScope,
    WorklistEnvelope,
    WorklistRow,
)
from app.repositories.base import InMemoryDocumentStore
from app.storage.base import ObjectMetadata, ObjectRef
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# CountingDocumentStore — wraps InMemoryDocumentStore, counts Firestore reads
# ---------------------------------------------------------------------------
class CountingDocumentStore:
    """DocumentStore proxy that counts ``get`` calls and query result reads."""

    def __init__(self, inner: InMemoryDocumentStore) -> None:
        self._inner = inner
        self.get_count = 0
        self.query_reads = 0

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        self.get_count += 1
        return await self._inner.get(collection, doc_id)

    async def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        await self._inner.set(collection, doc_id, data)

    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        return await self._inner.create(collection, doc_id, data)

    async def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        await self._inner.update(collection, doc_id, data)

    async def delete(self, collection: str, doc_id: str) -> None:
        await self._inner.delete(collection, doc_id)

    async def query(
        self,
        collection: str,
        *,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        results = await self._inner.query(collection, where=where, limit=limit)
        self.query_reads += len(results)
        return results

    def reset_counts(self) -> None:
        self.get_count = 0
        self.query_reads = 0

    @property
    def total_reads(self) -> int:
        return self.get_count + self.query_reads


# ---------------------------------------------------------------------------
# FakePixelStore — for signed URLs
# ---------------------------------------------------------------------------
class FakePixelStore:
    """Minimal ObjectStore whose signed URLs carry no-store headers."""

    def __init__(self) -> None:
        self._bucket = "test-pix"

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        return f"https://signed.example/{self._bucket}/{key}?ttl={ttl_seconds}"

    async def put(
        self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None
    ) -> ObjectRef:
        return ObjectRef(bucket=self._bucket, key=key)

    async def get_blob(self, key: str) -> bytes:
        return b""

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return b""

    async def delete(self, key: str) -> None:
        pass

    async def exists(self, key: str) -> bool:
        return False

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return []

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def object_metadata(self, key: str) -> ObjectMetadata:
        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket, key=key),
            size=0,
            content_type="",
            etag="",
            updated=datetime.now(UTC),
        )

    async def generate_signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int
    ) -> str:
        return f"https://upload.example/{key}"

    async def create_resumable_upload(
        self, key: str, content_type: str, expected_bytes: int
    ) -> str:
        return f"https://resumable.example/{key}"

    @property
    def supports_bucket_lock(self) -> bool:
        return True

    async def set_retention_policy(self, retention_days: int) -> None:
        pass


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------
AXIAL_IOP = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _instance(
    sop: str,
    idx: int,
    *,
    z: float | None = None,
    instance_number: int | None = None,
    iop: list[float] | None = None,
    size: int = 526336,
) -> Instance:
    return Instance(
        sop_instance_uid=sop,
        stack_index=idx,
        instance_number=instance_number,
        number_of_frames=1,
        image_position_patient=[0.0, 0.0, z] if z is not None else None,
        image_orientation_patient=iop
        if iop is not None
        else (AXIAL_IOP if z is not None else None),
        slice_location=z,
        size_bytes=size,
        object_path=f"studies/st_test/se_test/{idx:04d}.dcm",
    )


def _series(
    sid: str,
    n: int,
    *,
    spacing: float = 5.0,
    basis: StackOrderBasis = StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED,
    confidence: StackOrderConfidence = StackOrderConfidence.RELIABLE,
    irregular: bool = False,
    no_geometry: bool = False,
    reversed_instance_number: bool = False,
) -> Series:
    instances: list[Instance] = []
    for i in range(n):
        if no_geometry:
            inst = _instance(f"1.2.3.{i}", i, instance_number=i + 1)
            inst.image_position_patient = None
            inst.image_orientation_patient = None
            inst.slice_location = None
        elif irregular:
            z = i * spacing if i % 2 == 0 else i * spacing * 1.5
            inst = _instance(f"1.2.3.{i}", i, z=z, instance_number=i + 1)
        elif reversed_instance_number:
            inst = _instance(f"1.2.3.{i}", i, z=i * spacing, instance_number=n - i)
        else:
            inst = _instance(f"1.2.3.{i}", i, z=i * spacing, instance_number=i + 1)
        instances.append(inst)
    return Series(
        series_id=sid,
        study_id="st_test",
        study_instance_uid="1.2.840.113619.2.55.3.604688119.971",
        series_instance_uid=f"1.2.840.113619.2.55.3.{sid}",
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        stack_order_basis=basis,
        stack_order_confidence=confidence,
        instance_count=n,
        frame_count=n,
        is_multi_frame=False,
        instances=instances,
        created_at="2026-08-01T09:20:11Z",
    )


def _study_doc(
    *,
    study_id: str = "st_test",
    status: str = "UNREAD",
    assigned_to: dict[str, Any] | None = None,
    series_ids: list[str] | None = None,
    patient_key: str = "pk_test",
    patient_ref: str = "PT-2290513",
    modality: str = "CT",
    referring_physician: str = "",
    signed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "studyId": study_id,
        "patientKey": patient_key,
        "patientRef": patient_ref,
        "patientAgeSex": "41 F",
        "patientSex": "F",
        "patientName": "Doe, John",
        "patientBirthDate": "1985-03-02",
        "mrn": "MRN-4471",
        "accession": "ACC-2026-0731-4409",
        "modality": modality,
        "bodyPart": "CHEST",
        "description": "CT Chest with contrast",
        "studyDate": "2026-08-01T09:14:00Z",
        "referringPhysician": referring_physician,
        "clinicalHistory": "Persistent cough.",
        "status": status,
        "priority": "ROUTINE",
        "assignedTo": assigned_to,
        "seriesCount": len(series_ids) if series_ids else 0,
        "instanceCount": 412,
        "studyBytes": 216006656,
        "hasReport": False,
        "reportId": None,
        "signedAt": signed_at,
        "priorStudies": [],
        "seriesIds": series_ids or [],
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }


def _worklist_doc(items: list[dict[str, Any]], *, total_known: int | None = None) -> dict[str, Any]:
    return {
        "updatedAt": "2026-08-03T14:22:28Z",
        "count": len(items),
        "totalKnown": total_known if total_known is not None else len(items),
        "cap": 200,
        "oldestStudyDate": "2026-06-02T00:00:00Z",
        "generatedAt": "2026-08-03T14:22:28Z",
        "items": items,
    }


def _patient_doc() -> dict[str, Any]:
    return {
        "patientName": "Doe, John",
        "patientBirthDate": "1985-03-02",
        "mrn": "MRN-4471",
        "patientRef": "PT-2290513",
        "studyIds": ["st_test"],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def doc_store() -> CountingDocumentStore:
    return CountingDocumentStore(InMemoryDocumentStore())


@pytest.fixture
def audit_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def pixel_store() -> FakePixelStore:
    return FakePixelStore()


def _seed_4series_412(doc_store: CountingDocumentStore) -> None:
    """Seed a 4-series / 412-instance study + worklist + patient."""
    # 4 series with 103 instances each = 412 total
    series_ids = []
    for i in range(4):
        sid = f"se_{i:04d}"
        series_ids.append(sid)
        s = _series(sid, 103)
        asyncio.run(doc_store.set("series", sid, s.model_dump()))
    study = _study_doc(series_ids=series_ids)
    asyncio.run(doc_store.set("studies", "st_test", study))
    asyncio.run(doc_store.set("patients", "pk_test", _patient_doc()))
    asyncio.run(
        doc_store.set(
            "worklist_index",
            "current",
            _worklist_doc(
                [
                    {
                        "studyId": "st_test",
                        "patientKey": "pk_test",
                        "patientRef": "PT-2290513",
                        "patientAgeSex": "41 F",
                        "accession": "ACC-2026-0731-4409",
                        "modality": "CT",
                        "bodyPart": "CHEST",
                        "description": "CT Chest with contrast",
                        "studyDate": "2026-08-01T09:14:00Z",
                        "priority": "ROUTINE",
                        "status": "UNREAD",
                        "assignedTo": None,
                        "seriesCount": 4,
                        "instanceCount": 412,
                        "studyBytes": 216006656,
                        "hasReport": False,
                        "reportId": None,
                        "signedAt": None,
                        "updatedAt": "2026-08-01T09:20:11Z",
                    }
                ]
            ),
        )
    )


def _seed_large_series(doc_store: CountingDocumentStore, n: int, sid: str = "se_big") -> None:
    """Seed a study with one series of ``n`` instances."""
    s = _series(sid, n)
    asyncio.run(doc_store.set("series", sid, s.model_dump()))
    study = _study_doc(series_ids=[sid])
    asyncio.run(doc_store.set("studies", "st_test", study))
    asyncio.run(doc_store.set("patients", "pk_test", _patient_doc()))


@pytest.fixture
def studies_app(
    doc_store: CountingDocumentStore,
    audit_mirror: InMemoryAuditMirror,
    pixel_store: FakePixelStore,
) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    app.state.object_store = pixel_store
    app.state.document_store = doc_store
    app.state.audit_mirror = audit_mirror
    app.state.viewer_scopes = {}
    return app


@pytest.fixture
def client(studies_app: FastAPI) -> TestClient:
    return TestClient(studies_app)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Worklist — 1 Firestore read
# ---------------------------------------------------------------------------
class TestWorklist:
    def test_worklist_costs_exactly_1_read(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        doc_store.reset_counts()

        r = client.get("/api/v1/studies", headers=_auth())
        assert r.status_code == 200
        assert doc_store.get_count == 1
        assert doc_store.total_reads == 1

    def test_envelope_carries_required_fields(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        r = client.get("/api/v1/studies", headers=_auth())
        body = r.json()
        assert "studies" in body
        assert "truncated" in body
        assert "cap" in body
        assert "totalKnown" in body
        assert "generatedAt" in body
        assert "oldestStudyDate" in body
        assert body["cap"] == 200
        assert body["totalKnown"] == 1
        assert body["truncated"] is False

    def test_truncated_true_when_total_exceeds_cap(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        asyncio.run(doc_store.set("worklist_index", "current", _worklist_doc([], total_known=1284)))
        r = client.get("/api/v1/studies", headers=_auth())
        body = r.json()
        assert body["truncated"] is True
        assert body["totalKnown"] == 1284

    def test_worklist_rejects_limit_param(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies?limit=10", headers=_auth())
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_QUERY_PARAMETER"

    def test_worklist_rejects_cursor_param(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies?cursor=abc", headers=_auth())
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_QUERY_PARAMETER"

    def test_worklist_rejects_offset_param(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies?offset=5", headers=_auth())
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_QUERY_PARAMETER"

    def test_worklist_no_patient_name_or_dob_or_mrn(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        r = client.get("/api/v1/studies", headers=_auth())
        body = r.json()
        for item in body["studies"]:
            assert "patientName" not in item
            assert "patientBirthDate" not in item
            assert "mrn" not in item
            assert "patientRef" in item
            assert "patientAgeSex" in item


# ---------------------------------------------------------------------------
# 3. Search
# ---------------------------------------------------------------------------
class TestSearch:
    def test_zero_filters_returns_422(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/search", headers=_auth())
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "SEARCH_FILTER_REQUIRED"

    def test_limit_51_returns_422(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/search?patientRef=PT-1&limit=51", headers=_auth())
        assert r.status_code == 422

    def test_valid_query_returns_query_cost_reads(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        for i in range(3):
            asyncio.run(
                doc_store.set(
                    "studies",
                    f"st_{i}",
                    _study_doc(
                        study_id=f"st_{i}",
                        patient_ref="PT-1",
                        series_ids=[],
                    ),
                )
            )
        doc_store.reset_counts()
        r = client.get("/api/v1/studies/search?patientRef=PT-1&limit=10", headers=_auth())
        body = r.json()
        assert r.status_code == 200
        assert body["queryCostReads"] == doc_store.query_reads
        assert len(body["items"]) == 3

    def test_search_no_patient_name_or_dob_or_mrn(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        asyncio.run(doc_store.set("studies", "st_0", _study_doc(series_ids=[])))
        r = client.get("/api/v1/studies/search?patientRef=PT-2290513", headers=_auth())
        body = r.json()
        for item in body["items"]:
            assert "patientName" not in item
            assert "patientBirthDate" not in item
            assert "mrn" not in item


# ---------------------------------------------------------------------------
# 4. Series listing — 5 reads for 4-series study
# ---------------------------------------------------------------------------
class TestSeriesListing:
    def test_4series_412instance_costs_5_reads(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        doc_store.reset_counts()
        r = client.get("/api/v1/studies/st_test/series", headers=_auth())
        assert r.status_code == 200
        # 1 study read + 4 series reads = 5
        assert doc_store.get_count == 5
        assert doc_store.total_reads == 5

    def test_never_412_reads(self, client: TestClient, doc_store: CountingDocumentStore) -> None:
        _seed_4series_412(doc_store)
        doc_store.reset_counts()
        client.get("/api/v1/studies/st_test/series", headers=_auth())
        assert doc_store.total_reads < 100  # far less than 412


# ---------------------------------------------------------------------------
# 5. Access URLs — chunking
# ---------------------------------------------------------------------------
class TestAccessUrls:
    def test_824_instances_4_chunks(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_large_series(doc_store, 824)
        calls = 0
        next_idx: int | None = 0
        while next_idx is not None:
            r = client.get(
                f"/api/v1/studies/st_test/series/se_big/access-urls?fromStackIndex={next_idx}&count=250",
                headers=_auth(),
            )
            assert r.status_code == 200
            body = r.json()
            next_idx = body["nextFromStackIndex"]
            calls += 1
        assert calls == 4

    def test_1648_instances_7_chunks(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_large_series(doc_store, 1648)
        calls = 0
        next_idx: int | None = 0
        while next_idx is not None:
            r = client.get(
                f"/api/v1/studies/st_test/series/se_big/access-urls?fromStackIndex={next_idx}&count=250",
                headers=_auth(),
            )
            assert r.status_code == 200
            body = r.json()
            next_idx = body["nextFromStackIndex"]
            calls += 1
        assert calls == 7

    def test_count_251_returns_422(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_large_series(doc_store, 10)
        r = client.get(
            "/api/v1/studies/st_test/series/se_big/access-urls?count=251",
            headers=_auth(),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INSTANCE_CHUNK_TOO_LARGE"

    def test_cache_control_header_and_no_etag(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_large_series(doc_store, 5)
        r = client.get(
            "/api/v1/studies/st_test/series/se_big/access-urls?count=5",
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "private, no-store"
        assert "etag" not in {k.lower() for k in r.headers}

    def test_access_urls_response_shape(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_large_series(doc_store, 5)
        r = client.get(
            "/api/v1/studies/st_test/series/se_big/access-urls?fromStackIndex=0&count=3",
            headers=_auth(),
        )
        body = r.json()
        assert body["studyId"] == "st_test"
        assert body["fromStackIndex"] == 0
        assert body["count"] == 3
        assert body["nextFromStackIndex"] == 3
        assert body["seriesInstanceCount"] == 5
        assert len(body["instances"]) == 3
        assert "url" in body["instances"][0]
        assert "stackIndex" in body["instances"][0]
        assert "sopInstanceUid" in body["instances"][0]


# ---------------------------------------------------------------------------
# 10. Radiologist ownership — NOT_ASSIGNED
# ---------------------------------------------------------------------------
class TestRadiologistOwnership:
    def test_assigned_to_other_returns_403_not_assigned(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        # Re-seed study assigned to another radiologist
        study = _study_doc(
            status="IN_PROGRESS",
            assigned_to={"uid": "rad-other", "operatorId": "RAD-0099", "displayName": "Other"},
        )
        asyncio.run(doc_store.set("studies", "st_test", study))

        r = client.get("/api/v1/studies/st_test", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "NOT_ASSIGNED"

    def test_unassigned_unread_succeeds(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        r = client.get("/api/v1/studies/st_test", headers=_auth())
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 11. Admin → 403 PHI_ACCESS_FORBIDDEN on every study route
# ---------------------------------------------------------------------------
class TestAdminDenied:
    @pytest.fixture
    def admin_client(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
    ) -> TestClient:
        from app.main import create_app

        app = create_app()
        app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(
                uid="admin-1", role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED
            )
        )
        app.state.audit_object_store = StubAuditStore(locked=True)
        app.state.object_store = pixel_store
        app.state.document_store = doc_store
        app.state.audit_mirror = audit_mirror
        app.state.viewer_scopes = {}
        _seed_4series_412(doc_store)
        return TestClient(app)

    def test_admin_worklist_403(self, admin_client: TestClient) -> None:
        r = admin_client.get("/api/v1/studies", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_admin_search_403(self, admin_client: TestClient) -> None:
        r = admin_client.get("/api/v1/studies/search?patientRef=PT-1", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_admin_detail_403(self, admin_client: TestClient) -> None:
        r = admin_client.get("/api/v1/studies/st_test", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_admin_patient_identity_403(self, admin_client: TestClient) -> None:
        r = admin_client.get("/api/v1/studies/st_test/patient-identity", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_admin_series_403(self, admin_client: TestClient) -> None:
        r = admin_client.get("/api/v1/studies/st_test/series", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_admin_access_urls_403(self, admin_client: TestClient) -> None:
        r = admin_client.get("/api/v1/studies/st_test/series/se_0000/access-urls", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"


# ---------------------------------------------------------------------------
# 12. Viewer → 403 on non-SIGNED and SIGNED outside scope
# ---------------------------------------------------------------------------
class TestViewerAccess:
    def _viewer_app(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
        *,
        scope: ViewerScope | None = None,
    ) -> TestClient:
        from app.main import create_app

        app = create_app()
        app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(
                uid="viewer-1", role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED
            )
        )
        app.state.audit_object_store = StubAuditStore(locked=True)
        app.state.object_store = pixel_store
        app.state.document_store = doc_store
        app.state.audit_mirror = audit_mirror
        app.state.viewer_scopes = {"viewer-1": scope} if scope else {}
        return TestClient(app)

    def test_viewer_non_signed_403(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
    ) -> None:
        _seed_4series_412(doc_store)
        c = self._viewer_app(
            doc_store, audit_mirror, pixel_store, scope=ViewerScope(study_ids={"st_test"})
        )
        r = c.get("/api/v1/studies/st_test", headers=_auth())
        assert r.status_code == 403

    def test_viewer_signed_in_scope_succeeds(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
    ) -> None:
        _seed_4series_412(doc_store)
        study = _study_doc(
            status="SIGNED",
            signed_at="2026-08-02T10:00:00Z",
            series_ids=["se_0000", "se_0001", "se_0002", "se_0003"],
        )
        asyncio.run(doc_store.set("studies", "st_test", study))
        c = self._viewer_app(
            doc_store, audit_mirror, pixel_store, scope=ViewerScope(study_ids={"st_test"})
        )
        r = c.get("/api/v1/studies/st_test", headers=_auth())
        assert r.status_code == 200

    def test_viewer_signed_outside_scope_403(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
    ) -> None:
        _seed_4series_412(doc_store)
        study = _study_doc(
            status="SIGNED",
            signed_at="2026-08-02T10:00:00Z",
            series_ids=["se_0000", "se_0001", "se_0002", "se_0003"],
        )
        asyncio.run(doc_store.set("studies", "st_test", study))
        c = self._viewer_app(
            doc_store, audit_mirror, pixel_store, scope=ViewerScope(study_ids={"st_other"})
        )
        r = c.get("/api/v1/studies/st_test", headers=_auth())
        assert r.status_code == 403

    def test_viewer_empty_scope_reads_nothing(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
    ) -> None:
        _seed_4series_412(doc_store)
        study = _study_doc(
            status="SIGNED",
            signed_at="2026-08-02T10:00:00Z",
            series_ids=["se_0000", "se_0001", "se_0002", "se_0003"],
        )
        asyncio.run(doc_store.set("studies", "st_test", study))
        c = self._viewer_app(doc_store, audit_mirror, pixel_store)
        r = c.get("/api/v1/studies/st_test", headers=_auth())
        assert r.status_code == 403

    def test_viewer_patient_identity_403(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
    ) -> None:
        _seed_4series_412(doc_store)
        c = self._viewer_app(doc_store, audit_mirror, pixel_store)
        r = c.get("/api/v1/studies/st_test/patient-identity", headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"


# ---------------------------------------------------------------------------
# 13. Patient identity — the only name-releasing route
# ---------------------------------------------------------------------------
class TestPatientIdentity:
    def test_returns_patient_name_dob_mrn(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        r = client.get("/api/v1/studies/st_test/patient-identity", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["patientName"] == "Doe, John"
        assert body["patientBirthDate"] == "1985-03-02"
        assert body["mrn"] == "MRN-4471"

    def test_writes_patient_identity_viewed_audit(
        self,
        client: TestClient,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        _seed_4series_412(doc_store)
        client.get("/api/v1/studies/st_test/patient-identity", headers=_auth())
        events = [e for e in audit_mirror._events if e.event_type == "PATIENT_IDENTITY_VIEWED"]
        assert len(events) == 1
        assert events[0].detail["studyId"] == "st_test"

    def test_study_detail_has_no_patient_name(
        self, client: TestClient, doc_store: CountingDocumentStore
    ) -> None:
        _seed_4series_412(doc_store)
        r = client.get("/api/v1/studies/st_test", headers=_auth())
        body = r.json()
        assert "patientName" not in body
        assert "patientBirthDate" not in body
        assert "mrn" not in body


# ---------------------------------------------------------------------------
# 15. First-factor-only → 403 MFA_REQUIRED on every route
# ---------------------------------------------------------------------------
class TestMfaRequired:
    @pytest.fixture
    def mfa_client(
        self,
        doc_store: CountingDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        pixel_store: FakePixelStore,
    ) -> TestClient:
        from app.main import create_app

        app = create_app()
        app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.ENROLLED)
        )
        app.state.audit_object_store = StubAuditStore(locked=True)
        app.state.object_store = pixel_store
        app.state.document_store = doc_store
        app.state.audit_mirror = audit_mirror
        app.state.viewer_scopes = {}
        _seed_4series_412(doc_store)
        return TestClient(app)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/studies",
            "/api/v1/studies/search?patientRef=PT-1",
            "/api/v1/studies/st_test",
            "/api/v1/studies/st_test/patient-identity",
            "/api/v1/studies/st_test/series",
            "/api/v1/studies/st_test/series/se_0000/access-urls",
        ],
    )
    def test_mfa_required_on_every_route(self, mfa_client: TestClient, path: str) -> None:
        r = mfa_client.get(path, headers=_auth())
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"


# ---------------------------------------------------------------------------
# 404 — study not found
# ---------------------------------------------------------------------------
class TestNotFound:
    def test_study_not_found_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/studies/st_nonexistent", headers=_auth())
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 13. PHI model separation — only PatientIdentity declares patient name/DOB/MRN
# ---------------------------------------------------------------------------
class TestPhiModelSeparation:
    """No response model except PatientIdentity declares PHI name fields.

    Acceptance criterion 13: "A test greps the response models: no other
    response model declares a patientName, patientBirthDate, or mrn field."
    This is a static check on the model class definitions, not just the JSON
    responses — so a future field addition cannot silently leak PHI.
    """

    _PHI_FIELDS = frozenset({"patient_name", "patient_birth_date", "mrn"})

    @pytest.mark.parametrize(
        "model_cls",
        [
            WorklistEnvelope,
            WorklistRow,
            SearchResult,
            StudyDetail,
            SeriesListResponse,
            SeriesSummary,
            InstanceGeometry,
            AccessUrlChunk,
            AccessUrlEntry,
        ],
    )
    def test_no_phi_fields_on_wire_model(self, model_cls: type) -> None:
        field_names = set(model_cls.model_fields.keys())
        leaked = field_names & self._PHI_FIELDS
        assert not leaked, f"{model_cls.__name__} declares PHI fields: {leaked}"

    def test_patient_identity_is_the_only_model_with_phi(self) -> None:
        fields = set(PatientIdentity.model_fields.keys())
        assert "patient_name" in fields
        assert "patient_birth_date" in fields
        assert "mrn" in fields
