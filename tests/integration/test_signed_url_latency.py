# ruff: noqa: B008
"""Integration test — 250-URL chunk latency budget (§3.6, acceptance criterion 6).

Proves the semaphore concurrency works end-to-end through the FastAPI stack:
a 250-URL ``access-urls`` chunk completes in **under 1,500 ms** wall clock
against a fake ``signBlob`` with a 20 ms per-call delay.  Serialised this
would be 250 × 20 ms = 5,000 ms; with semaphore 32 it is ceil(250/32) × 20 ms
≈ 160 ms.  The budget proves the calls are actually concurrent, not serialised.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.models.series import Instance, Series, StackOrderBasis, StackOrderConfidence
from app.repositories.base import InMemoryDocumentStore
from app.storage.base import NO_STORE_CACHE_HEADERS, ObjectMetadata, ObjectRef
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# LatencyPixelStore — fake signBlob with configurable delay, tracks concurrency
# ---------------------------------------------------------------------------
class LatencyPixelStore:
    """ObjectStore whose signed-URL call sleeps ``delay_ms``.

    Tracks the maximum number of concurrent calls so the test can assert the
    semaphore ceiling is respected while still allowing concurrency.
    """

    def __init__(self, delay_ms: int = 20) -> None:
        self._delay_ms = delay_ms
        self._bucket = "test-pix"
        self._call_count = 0
        self._current_concurrent = 0
        self._max_concurrent = 0
        self._header_records: list[Mapping[str, str] | None] = []

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: Mapping[str, str] | None = None,
    ) -> str:
        self._call_count += 1
        self._header_records.append(response_headers)
        self._current_concurrent += 1
        self._max_concurrent = max(self._max_concurrent, self._current_concurrent)
        await asyncio.sleep(self._delay_ms / 1000)
        self._current_concurrent -= 1
        return f"https://signed.example/{self._bucket}/{key}?ttl={ttl_seconds}"

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def header_records(self) -> list[Mapping[str, str] | None]:
        return self._header_records

    # -- unused ObjectStore methods (stubs) ---------------------------------
    async def put(
        self, key: str, data: bytes, content_type: str, metadata: Mapping[str, str] | None = None
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
        metadata: Mapping[str, str] | None = None,
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


def _instances(n: int) -> list[Instance]:
    return [
        Instance(
            sop_instance_uid=f"1.2.3.{i}",
            stack_index=i,
            instance_number=i + 1,
            number_of_frames=1,
            image_position_patient=[0.0, 0.0, float(i * 5.0)],
            image_orientation_patient=AXIAL_IOP,
            slice_location=float(i * 5.0),
            size_bytes=526336,
            object_path=f"studies/st_lat/se_lat/{i:04d}.dcm",
        )
        for i in range(n)
    ]


def _seed_250_instances(doc_store: InMemoryDocumentStore) -> None:
    """Seed a study with one 250-instance series."""
    series = Series(
        series_id="se_lat",
        study_id="st_lat",
        study_instance_uid="1.2.840.113619.2.55.3.604688119.971",
        series_instance_uid="1.2.840.113619.2.55.3.se_lat",
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        stack_order_basis=StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED,
        stack_order_confidence=StackOrderConfidence.RELIABLE,
        instance_count=250,
        frame_count=250,
        is_multi_frame=False,
        instances=_instances(250),
        created_at="2026-08-01T09:20:11Z",
    )
    asyncio.run(doc_store.set("series", "se_lat", series.model_dump()))
    study = {
        "studyId": "st_lat",
        "patientKey": "pk_lat",
        "patientRef": "PT-LAT",
        "patientAgeSex": "55 M",
        "patientSex": "M",
        "patientName": "Test, Latency",
        "patientBirthDate": "1970-01-01",
        "mrn": "MRN-LAT",
        "accession": "ACC-LAT",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest",
        "studyDate": "2026-08-01T09:14:00Z",
        "referringPhysician": "",
        "clinicalHistory": "",
        "status": "UNREAD",
        "priority": "ROUTINE",
        "assignedTo": None,
        "seriesCount": 1,
        "instanceCount": 250,
        "studyBytes": 131584000,
        "hasReport": False,
        "reportId": None,
        "signedAt": None,
        "priorStudies": [],
        "seriesIds": ["se_lat"],
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }
    asyncio.run(doc_store.set("studies", "st_lat", study))


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
def pixel_store() -> LatencyPixelStore:
    return LatencyPixelStore(delay_ms=20)


@pytest.fixture
def client(
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
    pixel_store: LatencyPixelStore,
) -> TestClient:
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
    _seed_250_instances(doc_store)
    return TestClient(app)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestSignedUrlLatency:
    def test_250_urls_under_1500ms(
        self, client: TestClient, pixel_store: LatencyPixelStore
    ) -> None:
        """A 250-URL chunk completes in < 1,500 ms wall clock (criterion 6).

        Serialised: 250 × 20 ms = 5,000 ms.  With semaphore 32:
        ceil(250/32) × 20 ms ≈ 160 ms.  The < 1,500 ms budget proves the
        semaphore is actually concurrent end-to-end through the API.
        """
        start = time.perf_counter()
        r = client.get(
            "/api/v1/studies/st_lat/series/se_lat/access-urls?count=250",
            headers=_auth(),
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 250
        assert len(body["instances"]) == 250
        assert pixel_store.call_count == 250

        assert elapsed_ms < 1500, (
            f"250 URLs took {elapsed_ms:.0f}ms, expected < 1500ms (serialised would be ~5000ms)"
        )

    def test_semaphore_allows_concurrency(
        self, client: TestClient, pixel_store: LatencyPixelStore
    ) -> None:
        """The semaphore allows multiple concurrent signBlob calls (> 1)."""
        client.get(
            "/api/v1/studies/st_lat/series/se_lat/access-urls?count=250",
            headers=_auth(),
        )
        assert pixel_store.max_concurrent > 1, (
            "Semaphore did not allow concurrency — calls were serialised"
        )

    def test_semaphore_ceiling_respected(
        self, client: TestClient, pixel_store: LatencyPixelStore
    ) -> None:
        """The semaphore ceiling (32) is never exceeded."""
        client.get(
            "/api/v1/studies/st_lat/series/se_lat/access-urls?count=250",
            headers=_auth(),
        )
        assert pixel_store.max_concurrent <= 32, (
            f"Semaphore ceiling exceeded: {pixel_store.max_concurrent} > 32"
        )

    def test_each_url_carries_no_store_cache_headers(
        self, client: TestClient, pixel_store: LatencyPixelStore
    ) -> None:
        """Every signed-URL call carries Cache-Control: private, no-store (B10)."""
        client.get(
            "/api/v1/studies/st_lat/series/se_lat/access-urls?count=250",
            headers=_auth(),
        )
        assert len(pixel_store.header_records) == 250
        for headers in pixel_store.header_records:
            assert headers is not None
            assert headers.get("Cache-Control") == NO_STORE_CACHE_HEADERS["Cache-Control"]

    def test_response_carries_no_store_cache_header(self, client: TestClient) -> None:
        """The access-urls response itself carries Cache-Control: private, no-store."""
        r = client.get(
            "/api/v1/studies/st_lat/series/se_lat/access-urls?count=250",
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "private, no-store"
        assert "etag" not in {k.lower() for k in r.headers}
