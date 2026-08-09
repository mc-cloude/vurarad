# ruff: noqa: B008
"""Integration tests for quality-tier delivery and the manifest route (WP15).

End-to-end through the FastAPI app with an in-memory document store and a
byte-storing object store.  Covers:

- ``GET /studies/{studyId}/series/{seriesId}/manifest`` returns per-instance
  byte sizes at all three quality tiers (criterion 2).
- The manifest response carries ``Cache-Control: private, no-store`` (criterion 1).
- ``STUDY_IMAGES_ACCESSED`` audit event is written (criterion 1).
- Derived renditions are erased under ``derived/{studyId}/`` — erasing a study
  leaves ``derived/`` empty (criterion 3).
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
from app.repositories.base import InMemoryDocumentStore
from app.services.rendition_service import DERIVED_PREFIX, Quality, RenditionService
from app.storage.base import ObjectMetadata, ObjectRef
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user

STUDY_ID = "st_test"
SERIES_ID = "se_0001"


# ---------------------------------------------------------------------------
# ByteObjectStore — stores real bytes so manifest sizes are non-zero
# ---------------------------------------------------------------------------
class ByteObjectStore:
    """ObjectStore that stores bytes in a dict — for rendition/manifest tests."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._bucket = "test-pix"

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        self._objects[key] = data
        return ObjectRef(bucket=self._bucket, key=key)

    async def get_blob(self, key: str) -> bytes:
        return self._objects.get(key, b"")

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return self._objects.get(key, b"")[start:end]

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._objects

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return [
            ObjectRef(bucket=self._bucket, key=k)
            for k in sorted(self._objects)
            if k.startswith(prefix)
        ][:limit]

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        return f"https://signed.example/{key}?ttl={ttl_seconds}"

    async def generate_signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int
    ) -> str:
        return f"https://upload.example/{key}"

    async def create_resumable_upload(
        self, key: str, content_type: str, expected_bytes: int
    ) -> str:
        return f"https://resumable.example/{key}"

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        if src_key in self._objects:
            self._objects[dst_key] = self._objects[src_key]
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        if src_key in self._objects:
            self._objects[dst_key] = self._objects[src_key]
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def object_metadata(self, key: str) -> ObjectMetadata:
        data = self._objects.get(key, b"")
        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket, key=key),
            size=len(data),
            content_type="image/jpeg",
            etag="",
            updated=datetime.now(UTC),
        )

    @property
    def supports_bucket_lock(self) -> bool:
        return True

    async def set_retention_policy(self, retention_days: int) -> None:
        pass


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------
def _instance(sop: str, idx: int, *, size: int = 526336) -> Instance:
    return Instance(
        sop_instance_uid=sop,
        stack_index=idx,
        instance_number=idx + 1,
        size_bytes=size,
        object_path=f"dicom/{STUDY_ID}/{SERIES_ID}/{idx:04d}.dcm",
    )


def _series(n: int = 3) -> Series:
    instances = [_instance(f"1.2.3.{i}", i) for i in range(n)]
    return Series(
        series_id=SERIES_ID,
        study_id=STUDY_ID,
        study_instance_uid="1.2.840.113619.2.55.3.604688119.971",
        series_instance_uid=f"1.2.840.113619.2.55.3.{SERIES_ID}",
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        stack_order_basis=StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED,
        stack_order_confidence=StackOrderConfidence.RELIABLE,
        instance_count=n,
        frame_count=n,
        is_multi_frame=False,
        instances=instances,
        created_at="2026-08-01T09:20:11Z",
    )


def _study_doc() -> dict[str, Any]:
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
            "uid": "test-uid",
            "operatorId": "01HZTESTOPERATOR",
            "displayName": "Test User",
        },
        "seriesCount": 1,
        "instanceCount": 3,
        "studyBytes": 1579008,
        "hasReport": False,
        "reportId": None,
        "signedAt": None,
        "priorStudies": [],
        "seriesIds": [SERIES_ID],
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }


def _patient_doc() -> dict[str, Any]:
    return {
        "patientName": "Doe, John",
        "patientBirthDate": "1985-03-02",
        "mrn": "MRN-4471",
        "patientRef": "PT-001",
        "studyIds": [STUDY_ID],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    store = InMemoryDocumentStore()
    asyncio.run(store.set("studies", STUDY_ID, _study_doc()))
    asyncio.run(store.set("series", SERIES_ID, _series().model_dump()))
    asyncio.run(store.set("patients", "pk_test", _patient_doc()))
    return store


@pytest.fixture
def object_store() -> ByteObjectStore:
    return ByteObjectStore()


@pytest.fixture
def audit_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def app(
    doc_store: InMemoryDocumentStore,
    object_store: ByteObjectStore,
    audit_mirror: InMemoryAuditMirror,
) -> FastAPI:
    from app.main import create_app

    application = create_app()
    application.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    application.state.audit_object_store = StubAuditStore(locked=True)
    application.state.object_store = object_store
    application.state.document_store = doc_store
    application.state.audit_mirror = audit_mirror
    application.state.viewer_scopes = {}
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _put_thumbnail(object_store: ByteObjectStore, sop: str, data: bytes) -> None:
    key = RenditionService.derived_key(STUDY_ID, SERIES_ID, sop, Quality.THUMBNAIL)
    asyncio.run(object_store.put(key, data, "image/jpeg", {"study-id": STUDY_ID}))


# ---------------------------------------------------------------------------
# GET .../manifest — per-quality byte sizes (criterion 2)
# ---------------------------------------------------------------------------
class TestManifestRoute:
    def test_manifest_returns_three_quality_sizes(
        self,
        client: TestClient,
        object_store: ByteObjectStore,
    ) -> None:
        # Store thumbnails for instances 0 and 1.
        _put_thumbnail(object_store, "1.2.3.0", b"\x00" * 15_000)
        _put_thumbnail(object_store, "1.2.3.1", b"\x00" * 14_500)

        resp = client.get(
            f"/api/v1/studies/{STUDY_ID}/series/{SERIES_ID}/manifest",
            headers=_auth(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["studyId"] == STUDY_ID
        assert body["seriesUid"] == SERIES_ID
        assert body["instanceCount"] == 3
        assert len(body["instances"]) == 3

        e0 = body["instances"][0]
        assert e0["sopInstanceUid"] == "1.2.3.0"
        assert e0["stackIndex"] == 0
        assert e0["thumbnailBytes"] == 15_000
        assert e0["diagnosticBytes"] == 526336
        # Preview not yet generated → fallback ~120 KB.
        from app.services.rendition_service import PREVIEW_TARGET_BYTES

        assert e0["previewBytes"] == PREVIEW_TARGET_BYTES

        # Instance 2 has no stored thumbnail → 0 bytes.
        e2 = body["instances"][2]
        assert e2["thumbnailBytes"] == 0

    def test_manifest_carries_cache_control_no_store(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/studies/{STUDY_ID}/series/{SERIES_ID}/manifest",
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "private, no-store"

    def test_manifest_writes_study_images_accessed_audit(
        self,
        client: TestClient,
        audit_mirror: InMemoryAuditMirror,
    ) -> None:
        client.get(
            f"/api/v1/studies/{STUDY_ID}/series/{SERIES_ID}/manifest",
            headers=_auth(),
        )
        events = [e for e in audit_mirror._events if e.event_type == "STUDY_IMAGES_ACCESSED"]  # noqa: SLF001
        assert len(events) == 1
        detail = events[0].detail
        assert detail["studyId"] == STUDY_ID
        assert detail["seriesUid"] == SERIES_ID
        assert detail.get("manifest") is True

    def test_manifest_admin_gets_phi_forbidden(self, client: TestClient) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        resp = client.get(
            f"/api/v1/studies/{STUDY_ID}/series/{SERIES_ID}/manifest",
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"

    def test_manifest_missing_series_returns_404(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/studies/{STUDY_ID}/series/se_nonexistent/manifest",
            headers=_auth(),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Erasure — derived/ empty after erasing a study (criterion 3)
# ---------------------------------------------------------------------------
class TestDerivedErasure:
    def test_erase_for_study_empties_derived_prefix(
        self,
        object_store: ByteObjectStore,
    ) -> None:
        # Seed derived objects for two studies.
        for i in range(3):
            _put_thumbnail(object_store, f"1.2.3.{i}", b"\x00" * 10_000)
        # A second study's derived object.
        other_key = RenditionService.derived_key(
            "st_other", SERIES_ID, "1.2.3.0", Quality.THUMBNAIL
        )
        asyncio.run(object_store.put(other_key, b"\x00" * 10_000, "image/jpeg"))

        svc = RenditionService(object_store)
        count = asyncio.run(svc.erase_for_study(STUDY_ID))
        assert count == 3

        # derived/st_test/ is now empty.
        refs = asyncio.run(
            object_store.list_prefix(RenditionService.derived_prefix_for_study(STUDY_ID))
        )
        assert refs == []

        # derived/st_other/ survives.
        other_refs = asyncio.run(
            object_store.list_prefix(RenditionService.derived_prefix_for_study("st_other"))
        )
        assert len(other_refs) == 1

        # The entire derived/ prefix now has only the other study's object.
        all_derived = asyncio.run(object_store.list_prefix(DERIVED_PREFIX))
        assert len(all_derived) == 1
