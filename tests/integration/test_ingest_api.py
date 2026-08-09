# ruff: noqa: B008
"""Integration tests for the acquisition routes (§3.7 acceptance criteria).

End-to-end through the FastAPI app: ``POST /uploads`` mints resumable session
URLs, ``POST /uploads/{id}/complete`` runs the ingest job, and the two
``GET /ingest/jobs`` routes read job state.  Bytes never pass through the app —
the fake object store holds the quarantine objects and the ingest service reads
only header ranges and rewrites server-side.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydicom import Dataset, dcmwrite
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.repositories.base import InMemoryDocumentStore
from app.repositories.ingest_job_repo import LOCKS_COLLECTION, study_uid_hash
from app.repositories.series_repo import SeriesRepository
from app.services.audit_service import AuditService
from app.services.ingest_service import IngestService
from app.services.upload_service import UploadService
from app.storage.base import ObjectMetadata, ObjectRef
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user

CT_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.2"
STUDY_UID = "1.2.840.113619.2.55.3.604688119.971"
SERIES_UID = "1.2.840.113619.2.55.3.604688119.972"
_AXIAL_IOP = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
SLICE_SPACING = 5.0


# ---------------------------------------------------------------------------
# DICOM helpers
# ---------------------------------------------------------------------------
def make_ct_instance(
    sop_uid: str,
    *,
    study_uid: str = STUDY_UID,
    series_uid: str = SERIES_UID,
    z: float,
    instance_number: int,
) -> bytes:
    """Build a minimal valid CT DICOM Part 10 dataset as bytes (>= 1 KB)."""
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CT_SOP_CLASS
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "TEST123"
    ds.Modality = "CT"
    ds.InstanceNumber = instance_number
    ds.ImagePositionPatient = [0.0, 0.0, z]
    ds.ImageOrientationPatient = list(_AXIAL_IOP)
    ds.Rows = 64
    ds.Columns = 64
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\x00" * (64 * 64)

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CT_SOP_CLASS
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    ds.file_meta = file_meta
    ds.preamble = b"\x00" * 128

    buf = BytesIO()
    dcmwrite(buf, ds)
    return buf.getvalue()


def make_study_blobs(n: int, *, study_uid: str = STUDY_UID) -> list[bytes]:
    """Build ``n`` CT instances forming a regularly-spaced axial stack."""
    return [
        make_ct_instance(
            f"1.2.3.{i + 1}", study_uid=study_uid, z=i * SLICE_SPACING, instance_number=i + 1
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# FakeObjectStore — in-memory, records rewrites and Cache-Control
# ---------------------------------------------------------------------------
class FakeObjectStore:
    """In-memory ObjectStore that stores bytes and tracks Cache-Control."""

    def __init__(self, bucket_name: str = "test-pix") -> None:
        self._bucket_name = bucket_name
        self._data: dict[str, bytes] = {}
        self._meta: dict[str, dict[str, str]] = {}
        self._cache_control: dict[str, str | None] = {}
        self.rewrite_calls: list[tuple[str, str, str | None]] = []

    async def put(
        self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None
    ) -> ObjectRef:
        self._data[key] = data
        self._meta[key] = dict(metadata) if metadata else {}
        return ObjectRef(bucket=self._bucket_name, key=key)

    async def get_blob(self, key: str) -> bytes:
        return self._data.get(key, b"")

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return self._data.get(key, b"")[start:end]

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._cache_control.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        keys = sorted(k for k in self._data if k.startswith(prefix))
        return [ObjectRef(bucket=self._bucket_name, key=k) for k in keys[:limit]]

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        self._data[dst_key] = self._data.get(src_key, b"")
        return ObjectRef(bucket=self._bucket_name, key=dst_key)

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        self._data[dst_key] = self._data.get(src_key, b"")
        self._meta[dst_key] = dict(metadata) if metadata else {}
        self._cache_control[dst_key] = cache_control
        self.rewrite_calls.append((src_key, dst_key, cache_control))
        return ObjectRef(bucket=self._bucket_name, key=dst_key)

    async def object_metadata(self, key: str) -> ObjectMetadata:
        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket_name, key=key),
            size=len(self._data.get(key, b"")),
            content_type="application/dicom",
            etag="fake-etag",
            updated=datetime.now(UTC),
            metadata=self._meta.get(key, {}),
            cache_control=self._cache_control.get(key),
        )

    async def generate_signed_read_url(
        self, key: str, ttl_seconds: int, response_headers: dict[str, str] | None = None
    ) -> str:
        return f"https://fake.example/{self._bucket_name}/{key}"

    async def generate_signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int
    ) -> str:
        return f"https://fake.example/upload/{self._bucket_name}/{key}"

    async def create_resumable_upload(
        self, key: str, content_type: str, expected_bytes: int
    ) -> str:
        return f"https://fake.example/resumable/{self._bucket_name}/{key}"

    @property
    def supports_bucket_lock(self) -> bool:
        return True

    async def set_retention_policy(self, retention_days: int) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_store() -> FakeObjectStore:
    return FakeObjectStore()


@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


@pytest.fixture
def audit_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def ingest_app(
    fake_store: FakeObjectStore,
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    app.state.object_store = fake_store
    app.state.document_store = doc_store
    app.state.audit_mirror = audit_mirror
    return app


@pytest.fixture
def client(ingest_app: FastAPI) -> TestClient:
    return TestClient(ingest_app)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def _create_upload(client: TestClient, count: int, total_bytes: int, key: str) -> dict[str, Any]:
    r = client.post(
        "/api/v1/uploads",
        json={
            "sourceLabel": "CT-SCANNER-2",
            "expectedObjectCount": count,
            "expectedTotalBytes": total_bytes,
        },
        headers={**_auth(), "Idempotency-Key": key},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _complete(client: TestClient, upload_id: str, key: str) -> tuple[int, dict[str, Any]]:
    r = client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        headers={**_auth(), "Idempotency-Key": key},
    )
    return r.status_code, r.json()


def _seed_quarantine(store: FakeObjectStore, prefix: str, blobs: list[bytes]) -> None:
    for i, blob in enumerate(blobs):
        store._data[f"{prefix}{i + 1:04d}.dcm"] = blob


# ---------------------------------------------------------------------------
# Small read helpers
# ---------------------------------------------------------------------------
def _read_series(doc_store: InMemoryDocumentStore, study_id: str) -> list[Any]:
    import asyncio

    repo = SeriesRepository(doc_store)
    return asyncio.run(repo.get_series_for_study(study_id))


def _object_metadata(store: FakeObjectStore, key: str) -> ObjectMetadata:
    import asyncio

    return asyncio.run(store.object_metadata(key))


# ---------------------------------------------------------------------------
# POST /uploads
# ---------------------------------------------------------------------------
def test_upload_create_mints_session_urls(client: TestClient) -> None:
    data = _create_upload(client, count=5, total_bytes=5 * 5000, key="up-1")
    assert data["uploadId"].startswith("up_")
    assert data["quarantinePrefix"].startswith(f"quarantine/default/{data['uploadId']}/")
    urls = data["resumableSessionUrls"]
    assert len(urls) == 5
    assert [u["objectName"].rsplit("/", 1)[-1] for u in urls] == [
        "0001.dcm",
        "0002.dcm",
        "0003.dcm",
        "0004.dcm",
        "0005.dcm",
    ]
    assert all(u["sessionUrl"].startswith("https://") for u in urls)
    assert data["nextObjectIndex"] == 5


def test_upload_create_requires_idempotency_key(client: TestClient) -> None:
    r = client.post(
        "/api/v1/uploads",
        json={"sourceLabel": "x", "expectedObjectCount": 1, "expectedTotalBytes": 1024},
        headers=_auth(),
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /uploads/{id}/complete — happy path
# ---------------------------------------------------------------------------
def test_complete_succeeds_and_sets_cache_control(
    client: TestClient,
    fake_store: FakeObjectStore,
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
) -> None:
    blobs = make_study_blobs(5)
    total = sum(len(b) for b in blobs)
    session = _create_upload(client, count=5, total_bytes=total, key="ok-1")
    _seed_quarantine(fake_store, session["quarantinePrefix"], blobs)

    status, body = _complete(client, session["uploadId"], "ok-1")
    assert status == 202
    assert body["status"] == "SUCCEEDED"
    assert body["objectsTotal"] == 5
    assert body["studyId"] is not None

    # Every ingest writes a STUDY_INGESTED audit event through WP1's AuditService.
    assert len(audit_mirror._events) == 1
    assert audit_mirror._events[0].event_type == "STUDY_INGESTED"
    assert audit_mirror._events[0].detail["studyId"] == body["studyId"]
    assert audit_mirror._events[0].detail["status"] == "SUCCEEDED"

    # Cache-Control is exactly "private, no-store" on every rewritten object.
    assert len(fake_store.rewrite_calls) == 5
    for _src, dst, cc in fake_store.rewrite_calls:
        assert cc == "private, no-store"
        assert _object_metadata(fake_store, dst).cache_control == "private, no-store"

    # Dense, gapless, zero-based stack indices, geometric basis, reliable.
    series = _read_series(doc_store, body["studyId"])
    assert len(series) == 1
    inst = series[0].instances
    assert [i.stack_index for i in inst] == [0, 1, 2, 3, 4]
    assert series[0].stack_order_basis.value == "IMAGE_POSITION_PATIENT_PROJECTED"
    assert series[0].stack_order_confidence.value == "RELIABLE"


# ---------------------------------------------------------------------------
# A single unparseable object fails the whole job
# ---------------------------------------------------------------------------
def test_single_unparseable_object_fails_job(
    client: TestClient, fake_store: FakeObjectStore
) -> None:
    good = make_study_blobs(2)
    garbage = b"\x00" * 2048  # not DICOM, but >= 1 KB so it isn't a size failure
    blobs = good + [garbage]
    total = sum(len(b) for b in blobs)
    session = _create_upload(client, count=3, total_bytes=total, key="bad-1")
    _seed_quarantine(fake_store, session["quarantinePrefix"], blobs)

    status, body = _complete(client, session["uploadId"], "bad-1")
    assert status == 202
    assert body["status"] == "FAILED"
    assert len(body["errors"]) >= 1
    assert body["errors"][0]["reason"]


# ---------------------------------------------------------------------------
# Duplicate StudyInstanceUID → DUPLICATE with duplicateOf set
# ---------------------------------------------------------------------------
def test_duplicate_study_yields_duplicate_status(
    client: TestClient, fake_store: FakeObjectStore, doc_store: InMemoryDocumentStore
) -> None:
    first_blobs = make_study_blobs(3)
    s1 = _create_upload(client, count=3, total_bytes=sum(len(b) for b in first_blobs), key="dup-1")
    _seed_quarantine(fake_store, s1["quarantinePrefix"], first_blobs)
    st1, b1 = _complete(client, s1["uploadId"], "dup-1")
    assert st1 == 202 and b1["status"] == "SUCCEEDED"
    original_study_id = b1["studyId"]

    # Second upload: same StudyInstanceUID, one genuinely new instance.
    new_blob = make_ct_instance("1.2.3.99", z=3 * SLICE_SPACING, instance_number=99)
    s2 = _create_upload(client, count=1, total_bytes=len(new_blob), key="dup-2")
    _seed_quarantine(fake_store, s2["quarantinePrefix"], [new_blob])
    st2, b2 = _complete(client, s2["uploadId"], "dup-2")
    assert st2 == 202
    assert b2["status"] == "DUPLICATE"
    assert b2["duplicateOf"] == original_study_id

    # Only the new instance was added — the series now holds 4 instances.
    series = _read_series(doc_store, original_study_id)
    assert len(series) == 1
    sops = {i.sop_instance_uid for i in series[0].instances}
    assert sops == {f"1.2.3.{i}" for i in range(1, 4)} | {"1.2.3.99"}
    assert [i.stack_index for i in series[0].instances] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Concurrent job for the same study → 409 INGEST_IN_PROGRESS
# ---------------------------------------------------------------------------
def test_concurrent_job_returns_409(
    client: TestClient, fake_store: FakeObjectStore, doc_store: InMemoryDocumentStore
) -> None:
    blobs = make_study_blobs(3)
    total = sum(len(b) for b in blobs)
    session = _create_upload(client, count=3, total_bytes=total, key="concurrent-1")
    _seed_quarantine(fake_store, session["quarantinePrefix"], blobs)

    # Pre-create a non-expired lease held by a different owner.
    import asyncio

    asyncio.run(
        doc_store.set(
            LOCKS_COLLECTION,
            study_uid_hash(STUDY_UID),
            {
                "owner_token": "other-lease",
                "expire_at": time.time() + 3600,
                "acquired_at": time.time(),
            },
        )
    )

    status, body = _complete(client, session["uploadId"], "concurrent-1")
    assert status == 409
    assert body["error"]["code"] == "INGEST_IN_PROGRESS"


# ---------------------------------------------------------------------------
# Resume from checkpoint — no duplicates, dense stack indices
# ---------------------------------------------------------------------------
def test_resume_from_checkpoint(
    client: TestClient,
    fake_store: FakeObjectStore,
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
) -> None:
    import asyncio

    n = 10
    blobs = make_study_blobs(n)
    total = sum(len(b) for b in blobs)
    session = _create_upload(client, count=n, total_bytes=total, key="resume-1")
    _seed_quarantine(fake_store, session["quarantinePrefix"], blobs)
    upload_id = session["uploadId"]

    # Abort after 4 objects by calling the service directly (max_objects).
    upload_service = UploadService(fake_store, doc_store)
    series_repo = SeriesRepository(doc_store)
    from app.repositories.ingest_job_repo import IngestJobRepository

    job_repo = IngestJobRepository(doc_store)
    audit_service = AuditService(audit_mirror)
    svc = IngestService(upload_service, fake_store, series_repo, job_repo, audit_service)
    aborted = asyncio.run(
        svc.complete_upload(
            upload_id, idempotency_key="resume-1", actor="u1", second_factor=True, max_objects=4
        )
    )
    assert aborted.status.value == "RUNNING"
    assert aborted.last_checkpoint_index == 4

    # Resume via the API with the same Idempotency-Key.
    status, body = _complete(client, upload_id, "resume-1")
    assert status == 202
    assert body["status"] == "SUCCEEDED"
    assert body["lastCheckpointIndex"] == n

    # No duplicate instances and dense stack indices across the whole series.
    series = _read_series(doc_store, body["studyId"])
    assert len(series) == 1
    inst = series[0].instances
    assert len(inst) == n
    sops = [i.sop_instance_uid for i in inst]
    assert len(set(sops)) == n  # no duplicates
    assert [i.stack_index for i in inst] == list(range(n))


# ---------------------------------------------------------------------------
# GET /ingest/jobs/{jobId}
# ---------------------------------------------------------------------------
def test_get_job_by_id_and_404(client: TestClient, fake_store: FakeObjectStore) -> None:
    blobs = make_study_blobs(2)
    total = sum(len(b) for b in blobs)
    session = _create_upload(client, count=2, total_bytes=total, key="get-1")
    _seed_quarantine(fake_store, session["quarantinePrefix"], blobs)
    _st, body = _complete(client, session["uploadId"], "get-1")
    job_id = body["jobId"]

    r = client.get(f"/api/v1/ingest/jobs/{job_id}", headers=_auth())
    assert r.status_code == 200
    assert r.json()["jobId"] == job_id
    assert r.json()["status"] == "SUCCEEDED"

    r404 = client.get("/api/v1/ingest/jobs/job_does_not_exist", headers=_auth())
    assert r404.status_code == 404


# ---------------------------------------------------------------------------
# GET /ingest/jobs[?status=...]
# ---------------------------------------------------------------------------
def test_list_jobs_with_status_filter(client: TestClient, fake_store: FakeObjectStore) -> None:
    # One succeeded, one failed.
    good = make_study_blobs(2)
    s_ok = _create_upload(client, count=2, total_bytes=sum(len(b) for b in good), key="list-ok")
    _seed_quarantine(fake_store, s_ok["quarantinePrefix"], good)
    _complete(client, s_ok["uploadId"], "list-ok")

    garbage = b"\x00" * 2048
    s_bad = _create_upload(client, count=1, total_bytes=len(garbage), key="list-bad")
    _seed_quarantine(fake_store, s_bad["quarantinePrefix"], [garbage])
    _complete(client, s_bad["uploadId"], "list-bad")

    r_all = client.get("/api/v1/ingest/jobs", headers=_auth())
    assert r_all.status_code == 200
    all_items = r_all.json()
    assert all_items["nextCursor"] is None
    assert len(all_items["items"]) >= 2

    r_failed = client.get("/api/v1/ingest/jobs?status=FAILED", headers=_auth())
    assert r_failed.status_code == 200
    failed_items = r_failed.json()["items"]
    assert len(failed_items) >= 1
    assert all(j["status"] == "FAILED" for j in failed_items)

    r_succeeded = client.get("/api/v1/ingest/jobs?status=SUCCEEDED", headers=_auth())
    succ_items = r_succeeded.json()["items"]
    assert all(j["status"] == "SUCCEEDED" for j in succ_items)
    assert len(succ_items) >= 1


def test_list_jobs_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/ingest/jobs")
    assert r.status_code == 401
