# ruff: noqa: B008
"""Integration tests for native DICOMweb (QIDO/WADO/STOW) + auth + cross-tenant."""

from __future__ import annotations

from datetime import UTC
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydicom import Dataset, dcmwrite
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.dicomweb.models import (
    InstanceRecord,
    SeriesRecord,
    StudyRecord,
)
from app.storage.base import ObjectMetadata, ObjectRef
from tests.conftest import FakeTokenVerifier, StubAuditStore, make_user

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STUDY_UID = "1.2.840.113619.2.55.3.604688119.971"
SERIES_UID = "1.2.840.113619.2.55.3.604688119.972"
SOP_UID = "1.2.840.113619.2.55.3.604688119.973"
SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.2"
VALID_TOKEN = "valid-test-token"


# ---------------------------------------------------------------------------
# DICOM helpers
# ---------------------------------------------------------------------------
def make_minimal_dicom(
    study_uid: str = STUDY_UID,
    series_uid: str = SERIES_UID,
    sop_uid: str = SOP_UID,
    sop_class_uid: str = SOP_CLASS_UID,
) -> bytes:
    """Create a minimal valid DICOM dataset as bytes."""
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = sop_class_uid
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "TEST123"
    ds.Modality = "CT"

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = sop_class_uid
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    ds.file_meta = file_meta
    ds.preamble = b"\x00" * 128

    buf = BytesIO()
    dcmwrite(buf, ds)
    return buf.getvalue()


_DICOM_BYTES = make_minimal_dicom()


# ---------------------------------------------------------------------------
# FakeObjectStore — in-memory ObjectStore with call tracking
# ---------------------------------------------------------------------------
class FakeObjectStore:
    """In-memory ObjectStore that tracks get_blob and get_range calls."""

    def __init__(
        self,
        *,
        bucket_lock: bool = False,
        bucket_name: str = "test-pix",
    ) -> None:
        self._bucket_name = bucket_name
        self._locked = bucket_lock
        self._data: dict[str, bytes] = {}
        self._meta: dict[str, dict[str, str]] = {}
        self.get_blob_calls: int = 0
        self.get_range_calls: int = 0
        self.put_calls: int = 0

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        self.put_calls += 1
        self._data[key] = data
        self._meta[key] = dict(metadata) if metadata else {}
        return ObjectRef(bucket=self._bucket_name, key=key)

    async def get_blob(self, key: str) -> bytes:
        self.get_blob_calls += 1
        return self._data.get(key, b"")

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        self.get_range_calls += 1
        return self._data.get(key, b"")[start:end]

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return [
            ObjectRef(bucket=self._bucket_name, key=k) for k in self._data if k.startswith(prefix)
        ][:limit]

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        self._data[dst_key] = self._data.get(src_key, b"")
        return ObjectRef(bucket=self._bucket_name, key=dst_key)

    async def object_metadata(self, key: str) -> ObjectMetadata:
        from datetime import datetime

        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket_name, key=key),
            size=len(self._data.get(key, b"")),
            content_type="application/dicom",
            etag="fake-etag",
            updated=datetime.now(UTC),
            metadata=self._meta.get(key, {}),
        )

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        return f"https://fake.example/{self._bucket_name}/{key}"

    async def generate_signed_upload_url(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int,
    ) -> str:
        return f"https://fake.example/upload/{self._bucket_name}/{key}"

    async def create_resumable_upload(
        self,
        key: str,
        content_type: str,
        expected_bytes: int,
    ) -> str:
        return f"https://fake.example/resumable/{self._bucket_name}/{key}"

    @property
    def supports_bucket_lock(self) -> bool:
        return self._locked

    async def set_retention_policy(self, retention_days: int) -> None:
        pass


# ---------------------------------------------------------------------------
# FakeDicomMetadataStore — in-memory, tenant-scoped
# ---------------------------------------------------------------------------
class FakeDicomMetadataStore:
    """In-memory tenant-scoped metadata store."""

    def __init__(self) -> None:
        self._studies: dict[str, StudyRecord] = {}
        self._series: dict[str, SeriesRecord] = {}
        self._instances: dict[str, InstanceRecord] = {}

    async def put_study(self, study: StudyRecord) -> None:
        self._studies[study.study_uid] = study

    async def put_series(self, series: SeriesRecord) -> None:
        key = f"{series.study_uid}_{series.series_uid}"
        self._series[key] = series

    async def put_instance(self, instance: InstanceRecord) -> None:
        key = f"{instance.study_uid}_{instance.series_uid}_{instance.sop_uid}"
        self._instances[key] = instance

    async def get_study(self, study_uid: str, tenant_id: str) -> StudyRecord | None:
        study = self._studies.get(study_uid)
        if study is None or study.tenant_id != tenant_id:
            return None
        return study

    async def get_series(
        self, study_uid: str, series_uid: str, tenant_id: str
    ) -> SeriesRecord | None:
        series = self._series.get(f"{study_uid}_{series_uid}")
        if series is None or series.tenant_id != tenant_id:
            return None
        return series

    async def get_instance(
        self,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        tenant_id: str,
    ) -> InstanceRecord | None:
        inst = self._instances.get(f"{study_uid}_{series_uid}_{sop_uid}")
        if inst is None or inst.tenant_id != tenant_id:
            return None
        return inst

    async def query_studies(
        self, tenant_id: str, limit: int, offset: str | None
    ) -> list[StudyRecord]:
        return [s for s in self._studies.values() if s.tenant_id == tenant_id][:limit]

    async def query_series(
        self, study_uid: str, tenant_id: str, limit: int, offset: str | None
    ) -> list[SeriesRecord]:
        return [
            s
            for s in self._series.values()
            if s.tenant_id == tenant_id and s.study_uid == study_uid
        ][:limit]

    async def query_instances(
        self,
        study_uid: str,
        series_uid: str,
        tenant_id: str,
        limit: int,
        offset: str | None,
    ) -> list[InstanceRecord]:
        return [
            i
            for i in self._instances.values()
            if i.tenant_id == tenant_id and i.study_uid == study_uid and i.series_uid == series_uid
        ][:limit]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_store() -> FakeObjectStore:
    return FakeObjectStore(bucket_lock=True, bucket_name="test-pix")


@pytest.fixture
def fake_meta() -> FakeDicomMetadataStore:
    store = FakeDicomMetadataStore()
    # Seed a study/series/instance
    store._studies[STUDY_UID] = StudyRecord(
        study_uid=STUDY_UID,
        patient_id="TEST123",
        patient_name="TEST^PATIENT",
        study_date="20240101",
        study_time="120000",
        accession_number="ACC001",
        modalities_in_study=["CT"],
        study_description="Test Study",
        tenant_id="default",
        num_series=1,
        num_instances=1,
    )
    store._series[f"{STUDY_UID}_{SERIES_UID}"] = SeriesRecord(
        study_uid=STUDY_UID,
        series_uid=SERIES_UID,
        modality="CT",
        series_number=1,
        series_description="Test Series",
        tenant_id="default",
        num_instances=1,
    )
    store._instances[f"{STUDY_UID}_{SERIES_UID}_{SOP_UID}"] = InstanceRecord(
        study_uid=STUDY_UID,
        series_uid=SERIES_UID,
        sop_uid=SOP_UID,
        sop_class_uid=SOP_CLASS_UID,
        instance_number=1,
        rows=512,
        columns=512,
        num_frames=1,
        tenant_id="default",
        object_ref="dicom/" + f"{STUDY_UID}/{SERIES_UID}/{SOP_UID}.dcm",
        pixel_data_offset=0,
        frame_offsets=[0, 65536],
    )
    return store


@pytest.fixture
def dicomweb_app(
    fake_store: FakeObjectStore,
    fake_meta: FakeDicomMetadataStore,
) -> FastAPI:
    from app.main import create_app

    app = create_app()
    verifier = FakeTokenVerifier(
        default_user=make_user(
            role=Role.RADIOLOGIST,
            mfa_state=SecondFactorState.VERIFIED,
        )
    )
    app.state.token_verifier = verifier
    app.state.object_store = fake_store
    app.state.dicom_metadata_store = fake_meta
    app.state.audit_object_store = StubAuditStore(locked=True)
    # Seed pixel data in the fake store
    fake_store._data[f"dicom/{STUDY_UID}/{SERIES_UID}/{SOP_UID}.dcm"] = _DICOM_BYTES
    return app


@pytest.fixture
def dicomweb_client(dicomweb_app: FastAPI) -> TestClient:
    return TestClient(dicomweb_app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def _stow_body(dicom_bytes: bytes, boundary: str = "test-boundary") -> bytes:
    """Build a multipart/related body for STOW-RS."""
    parts = [
        f"--{boundary}\r\n".encode(),
        b"Content-Type: application/dicom\r\n\r\n",
        dicom_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts)


# ---------------------------------------------------------------------------
# QIDO-RS
# ---------------------------------------------------------------------------
class TestQIDO:
    def test_qido_studies_returns_dicom_json(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get("/dicomweb/studies", headers=_auth_headers())
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert "0020000D" in data[0]  # StudyInstanceUID

    def test_qido_series_returns_dicom_json(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}/series",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert "0020000E" in data[0]  # SeriesInstanceUID

    def test_qido_instances_returns_dicom_json(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}/instances",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert "00080018" in data[0]  # SOPInstanceUID


# ---------------------------------------------------------------------------
# WADO-RS
# ---------------------------------------------------------------------------
class TestWADO:
    def test_wado_study_returns_multipart(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        assert "multipart/related" in r.headers.get("content-type", "")
        assert _DICOM_BYTES in r.content

    def test_wado_series_returns_multipart(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        assert "multipart/related" in r.headers.get("content-type", "")

    def test_wado_instance_returns_multipart(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}/instances/{SOP_UID}",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        assert "multipart/related" in r.headers.get("content-type", "")
        assert _DICOM_BYTES in r.content

    def test_wado_frames_use_get_range(
        self,
        dicomweb_client: TestClient,
        fake_store: FakeObjectStore,
    ) -> None:
        """AC7: frame requests must use get_range, not get_blob."""
        fake_store.get_blob_calls = 0
        fake_store.get_range_calls = 0
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}/instances/{SOP_UID}/frames/1",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        assert fake_store.get_range_calls >= 1, "frame request must use get_range"
        assert fake_store.get_blob_calls == 0, "frame request must NOT use get_blob"

    def test_wado_metadata_returns_json(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}/instances/{SOP_UID}/metadata",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert "00080018" in data[0]  # SOPInstanceUID

    def test_wado_rendered_returns_jpeg(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}/instances/{SOP_UID}/rendered",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content[:2] == b"\xff\xd8"  # JPEG SOI marker


# ---------------------------------------------------------------------------
# STOW-RS
# ---------------------------------------------------------------------------
class TestSTOW:
    def test_stow_stores_to_quarantine(
        self,
        dicomweb_client: TestClient,
        fake_store: FakeObjectStore,
    ) -> None:
        body = _stow_body(_DICOM_BYTES)
        r = dicomweb_client.post(
            "/dicomweb/studies",
            content=body,
            headers={
                **_auth_headers(),
                "Content-Type": (
                    'multipart/related; type="application/dicom"; boundary=test-boundary'
                ),
            },
        )
        assert r.status_code == 200
        # Verify the instance was written to quarantine, not dicom
        quarantined = [k for k in fake_store._data if k.startswith("quarantine/")]
        assert len(quarantined) == 1, "STOW must write to quarantine prefix"
        dicom_keys = [k for k in fake_store._data if k.startswith("dicom/")]
        # Only the pre-seeded dicom key should exist
        assert len(dicom_keys) == 1

    def test_stow_returns_200_on_success(self, dicomweb_client: TestClient) -> None:
        body = _stow_body(_DICOM_BYTES)
        r = dicomweb_client.post(
            "/dicomweb/studies",
            content=body,
            headers={
                **_auth_headers(),
                "Content-Type": (
                    'multipart/related; type="application/dicom"; boundary=test-boundary'
                ),
            },
        )
        assert r.status_code == 200
        assert "application/dicom+json" in r.headers.get("content-type", "")
        data = r.json()
        assert "00081199" in data  # ReferencedSOPSequence

    def test_stow_empty_body_returns_400(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.post(
            "/dicomweb/studies",
            content=b"",
            headers={
                **_auth_headers(),
                "Content-Type": (
                    'multipart/related; type="application/dicom"; boundary=test-boundary'
                ),
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# DICOMweb authentication
# ---------------------------------------------------------------------------
class TestDICOMwebAuth:
    def test_no_token_returns_401(self, dicomweb_client: TestClient) -> None:
        r = dicomweb_client.get("/dicomweb/studies")
        assert r.status_code == 401

    def test_unenrolled_mfa_returns_403(self, dicomweb_app: FastAPI) -> None:
        verifier = dicomweb_app.state.token_verifier
        assert isinstance(verifier, FakeTokenVerifier)
        verifier.default_user = make_user(
            role=Role.RADIOLOGIST,
            mfa_state=SecondFactorState.UNENROLLED,
        )
        client = TestClient(dicomweb_app)
        r = client.get("/dicomweb/studies", headers=_auth_headers())
        assert r.status_code == 403

    def test_viewer_cannot_import(self, dicomweb_app: FastAPI) -> None:
        """Viewer lacks STUDY_IMPORT → POST /studies is 403."""
        verifier = dicomweb_app.state.token_verifier
        assert isinstance(verifier, FakeTokenVerifier)
        verifier.default_user = make_user(
            role=Role.VIEWER,
            mfa_state=SecondFactorState.VERIFIED,
        )
        client = TestClient(dicomweb_app)
        body = _stow_body(_DICOM_BYTES)
        r = client.post(
            "/dicomweb/studies",
            content=body,
            headers={
                **_auth_headers(),
                "Content-Type": (
                    'multipart/related; type="application/dicom"; boundary=test-boundary'
                ),
            },
        )
        assert r.status_code == 403

    def test_viewer_can_search(self, dicomweb_app: FastAPI) -> None:
        """Viewer has STUDY_SEARCH → GET /studies is 200."""
        verifier = dicomweb_app.state.token_verifier
        assert isinstance(verifier, FakeTokenVerifier)
        verifier.default_user = make_user(
            role=Role.VIEWER,
            mfa_state=SecondFactorState.VERIFIED,
        )
        client = TestClient(dicomweb_app)
        r = client.get("/dicomweb/studies", headers=_auth_headers())
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Cross-tenant isolation (404, never 403)
# ---------------------------------------------------------------------------
class TestCrossTenant:
    @pytest.fixture
    def other_tenant_app(
        self,
        fake_store: FakeObjectStore,
        fake_meta: FakeDicomMetadataStore,
    ) -> FastAPI:
        from app.main import create_app

        app = create_app()
        user = make_user(
            role=Role.RADIOLOGIST,
            mfa_state=SecondFactorState.VERIFIED,
        )
        object.__setattr__(user, "tenant_id", "other-tenant")
        verifier = FakeTokenVerifier(default_user=user)
        app.state.token_verifier = verifier
        app.state.object_store = fake_store
        app.state.dicom_metadata_store = fake_meta
        app.state.audit_object_store = StubAuditStore(locked=True)
        return app

    def test_cross_tenant_study_returns_404(self, other_tenant_app: FastAPI) -> None:
        """Accessing another tenant's study returns 404, not 403."""
        client = TestClient(other_tenant_app)
        r = client.get(
            f"/dicomweb/studies/{STUDY_UID}/series",
            headers=_auth_headers(),
        )
        assert r.status_code == 404

    def test_cross_tenant_instance_returns_404(self, other_tenant_app: FastAPI) -> None:
        client = TestClient(other_tenant_app)
        r = client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}/instances/{SOP_UID}",
            headers=_auth_headers(),
        )
        assert r.status_code == 404

    def test_cross_tenant_metadata_returns_404(self, other_tenant_app: FastAPI) -> None:
        client = TestClient(other_tenant_app)
        r = client.get(
            f"/dicomweb/studies/{STUDY_UID}/series/{SERIES_UID}/instances/{SOP_UID}/metadata",
            headers=_auth_headers(),
        )
        assert r.status_code == 404
