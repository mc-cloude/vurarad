# ruff: noqa: B008
"""Benchmark for the ingest path (§3.7 acceptance criterion 10).

A 500-object synthetic study completes under 90 s, and a job aborted at object
200 resumes correctly with no duplicates and dense stack indices.  A
latency-injecting fake object store stands in for GCS so the test exercises the
real header-range + server-side-rewrite flow without network I/O.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.repositories.base import InMemoryDocumentStore
from app.repositories.ingest_job_repo import IngestJobRepository
from app.repositories.series_repo import SeriesRepository
from app.services.audit_service import AuditService
from app.services.ingest_service import IngestService
from app.services.upload_service import UploadService
from tests.conftest import FakeTokenVerifier, StubAuditStore, make_user
from tests.integration.test_ingest_api import (
    FakeObjectStore,
    _complete,
    _create_upload,
    _read_series,
    _seed_quarantine,
    make_study_blobs,
)

pytestmark = pytest.mark.slow

N_OBJECTS = 500
ABORT_AT = 200
BUDGET_SECONDS = 90


class LatencyObjectStore(FakeObjectStore):
    """FakeObjectStore that injects a small per-I/O delay to emulate GCS."""

    def __init__(self, delay: float = 0.002, bucket_name: str = "bench-pix") -> None:
        super().__init__(bucket_name=bucket_name)
        self._delay = delay

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        await asyncio.sleep(self._delay)
        return await super().get_range(key, start, end)

    async def object_metadata(self, key: str):  # type: ignore[no-untyped-def]
        await asyncio.sleep(self._delay)
        return await super().object_metadata(key)

    async def rewrite(  # type: ignore[no-untyped-def]
        self, src_key: str, dst_key: str, *, cache_control=None, metadata=None
    ):
        await asyncio.sleep(self._delay)
        return await super().rewrite(
            src_key, dst_key, cache_control=cache_control, metadata=metadata
        )

    async def list_prefix(self, prefix: str, limit: int = 1000):  # type: ignore[no-untyped-def]
        await asyncio.sleep(self._delay)
        return await super().list_prefix(prefix, limit=limit)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def bench_store() -> LatencyObjectStore:
    return LatencyObjectStore()


@pytest.fixture
def bench_doc_store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


@pytest.fixture
def bench_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def bench_app(
    bench_store: LatencyObjectStore,
    bench_doc_store: InMemoryDocumentStore,
    bench_mirror: InMemoryAuditMirror,
) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    app.state.object_store = bench_store
    app.state.document_store = bench_doc_store
    app.state.audit_mirror = bench_mirror
    return app


@pytest.fixture
def bench_client(bench_app: FastAPI) -> TestClient:
    return TestClient(bench_app)


def _build_service(
    store: LatencyObjectStore,
    doc_store: InMemoryDocumentStore,
    mirror: InMemoryAuditMirror,
) -> IngestService:
    upload_service = UploadService(store, doc_store)
    return IngestService(
        upload_service,
        store,
        SeriesRepository(doc_store),
        IngestJobRepository(doc_store),
        AuditService(mirror),
    )


# ---------------------------------------------------------------------------
# 500-object study completes under 90 s
# ---------------------------------------------------------------------------
def test_500_object_study_completes_under_budget(
    bench_client: TestClient,
    bench_store: LatencyObjectStore,
    bench_doc_store: InMemoryDocumentStore,
) -> None:
    blobs = make_study_blobs(N_OBJECTS)
    total = sum(len(b) for b in blobs)
    session = _create_upload(bench_client, count=N_OBJECTS, total_bytes=total, key="bench-1")
    _seed_quarantine(bench_store, session["quarantinePrefix"], blobs)

    start = time.perf_counter()
    status, body = _complete(bench_client, session["uploadId"], "bench-1")
    elapsed = time.perf_counter() - start

    assert status == 202
    assert body["status"] == "SUCCEEDED"
    assert elapsed < BUDGET_SECONDS, f"ingest took {elapsed:.1f}s, budget {BUDGET_SECONDS}s"
    assert body["objectsTotal"] == N_OBJECTS

    series = _read_series(bench_doc_store, body["studyId"])
    assert len(series) == 1
    inst = series[0].instances
    assert len(inst) == N_OBJECTS
    assert [i.stack_index for i in inst] == list(range(N_OBJECTS))


# ---------------------------------------------------------------------------
# Abort at object 200 resumes correctly
# ---------------------------------------------------------------------------
def test_abort_at_200_resumes_correctly(
    bench_client: TestClient,
    bench_store: LatencyObjectStore,
    bench_doc_store: InMemoryDocumentStore,
    bench_mirror: InMemoryAuditMirror,
) -> None:
    blobs = make_study_blobs(N_OBJECTS)
    total = sum(len(b) for b in blobs)
    session = _create_upload(bench_client, count=N_OBJECTS, total_bytes=total, key="bench-2")
    _seed_quarantine(bench_store, session["quarantinePrefix"], blobs)
    upload_id = session["uploadId"]

    # Abort after 200 objects (service-level max_objects).
    svc = _build_service(bench_store, bench_doc_store, bench_mirror)
    aborted = asyncio.run(
        svc.complete_upload(
            upload_id,
            idempotency_key="bench-2",
            actor="u1",
            second_factor=True,
            max_objects=ABORT_AT,
        )
    )
    assert aborted.status.value == "RUNNING"
    assert aborted.last_checkpoint_index == ABORT_AT

    # Resume via the API with the same Idempotency-Key.
    status, body = _complete(bench_client, upload_id, "bench-2")
    assert status == 202
    assert body["status"] == "SUCCEEDED"
    assert body["lastCheckpointIndex"] == N_OBJECTS

    series = _read_series(bench_doc_store, body["studyId"])
    assert len(series) == 1
    inst = series[0].instances
    assert len(inst) == N_OBJECTS
    sops = [i.sop_instance_uid for i in inst]
    assert len(set(sops)) == N_OBJECTS  # no duplicates after resume
    assert [i.stack_index for i in inst] == list(range(N_OBJECTS))
