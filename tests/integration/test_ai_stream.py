# ruff: noqa: B008
"""Integration tests for the AI streaming API (§3.21 AI — criteria 14–18).

End-to-end through the FastAPI app with an in-memory document store and a fake
genai client injected via ``app.state.gemini_service``.  Never calls the real
API.  Covers:

- SSE frame sequence: ``meta → delta(s) → done`` (criterion 14);
- error sequence: ``meta → error`` (criterion 14);
- budget refusal returns an ``error`` frame with ``AI_BUDGET_EXCEEDED``;
- a timeout yields an ``error`` frame with ``AI_UPSTREAM_TIMEOUT`` (criterion 16);
- admin → ``403 PHI_ACCESS_FORBIDDEN`` (criterion 18);
- the 11th per-user request → ``429`` with ``Retry-After`` (criterion 17).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import types

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.core.ratelimit import BUCKET_LIMITS, RateLimitBucket, RateLimiter
from app.models.errors import ErrorCode
from app.repositories.base import InMemoryDocumentStore
from app.services.gemini_service import GeminiService
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# Fakes for the genai SDK
# ---------------------------------------------------------------------------
class _Usage:
    def __init__(
        self,
        *,
        prompt: int | None = 100,
        candidates: int | None = 50,
        thoughts: int | None = 10,
        cached: int | None = 5,
        total: int | None = 165,
    ) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts
        self.cached_content_token_count = cached
        self.total_token_count = total


class _Candidate:
    def __init__(self, finish_reason: types.FinishReason | None) -> None:
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(
        self,
        text: str | None,
        *,
        usage: _Usage | None = None,
        finish_reason: types.FinishReason | None = None,
        model_version: str = "gemini-2.5-flash-001",
    ) -> None:
        self.text = text
        self.usage_metadata = usage
        self.candidates = [_Candidate(finish_reason)] if finish_reason is not None else None
        self.model_version = model_version


class _AsyncModels:
    def __init__(self, chunks: list[_Chunk], raise_exc: BaseException | None = None) -> None:
        self._chunks = chunks
        self._raise = raise_exc
        self.calls = 0

    async def generate_content_stream(
        self,
        *,
        model: str,
        contents: str,
        config: Any = None,
    ) -> AsyncIterator[_Chunk]:
        self.calls += 1
        chunks, raise_exc = self._chunks, self._raise

        async def _iterator() -> AsyncIterator[_Chunk]:
            for chunk in chunks:
                yield chunk
            if raise_exc is not None:
                raise raise_exc

        return _iterator()


class _Aio:
    def __init__(self, models: _AsyncModels) -> None:
        self.models = models


class _Client:
    def __init__(self, models: _AsyncModels) -> None:
        self.aio = _Aio(models)


class _Budget:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.recorded: list[tuple[str, dict[str, int]]] = []

    async def check_budget(self, tenant_id: str) -> bool:
        return self.allowed

    async def record_usage(self, tenant_id: str, usage: dict[str, int]) -> None:
        self.recorded.append((tenant_id, dict(usage)))


def _service(models: _AsyncModels, budget: _Budget | None = None) -> GeminiService:
    return GeminiService(
        project="vurarad-test",
        location="us-central1",
        client=_Client(models),
        budget_repo=budget,
    )


# A 2-section report streamed across three chunks (mid-marker splits included).
_REPORT_CHUNKS = [
    _Chunk("<<SECTION:Findings>>The liver is normal in size."),
    _Chunk(" No focal lesions are seen.\n"),
    _Chunk(
        "<<SECTION:Impression>>No acute findings.",
        usage=_Usage(prompt=120, candidates=60, thoughts=15, cached=5, total=195),
        finish_reason=types.FinishReason.STOP,
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _study_doc(study_id: str = "st_test") -> dict[str, Any]:
    return {
        "studyId": study_id,
        "patientKey": "pk-secret",
        "patientRef": "PT-SECRET",
        "patientAgeSex": "41 F",
        "patientSex": "F",
        "patientName": "Doe, John",
        "patientBirthDate": "1985-03-02",
        "mrn": "MRN-4471",
        "accession": "ACC-SECRET",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest with contrast",
        "studyDate": "2026-08-01T09:14:00Z",
        "status": "UNREAD",
        "priority": "ROUTINE",
        "assignedTo": {"uid": "test-uid", "operatorId": "01HZTESTOPERATOR", "displayName": "Test"},
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


def _seed(doc_store: InMemoryDocumentStore) -> None:
    import asyncio as _aio

    _aio.run(doc_store.set("studies", "st_test", _study_doc()))
    # A confirmed finding (snake_case, as FindingService stores it).
    _aio.run(
        doc_store.set(
            "findings",
            "fd_1",
            {
                "finding_id": "fd_1",
                "study_id": "st_test",
                "category": "ANATOMICAL_MEASUREMENT",
                "regulatory_class": "MEASUREMENT",
                "clinical_use_allowed": True,
                "disposition": {"state": "CONFIRMED", "confirmed_text": "Liver volume 1450 mL"},
            },
        )
    )
    # A dictation segment (snake_case + study_id, as DictationService stores it).
    _aio.run(
        doc_store.set(
            "dictation_segments",
            "dc_1:mut-1",
            {
                "session_id": "dc_1",
                "mutation_id": "mut-1",
                "seq": 1,
                "at": "2026-08-01T10:00:00Z",
                "source": "SPEECH",
                "text": "No acute findings on dictation.",
                "study_id": "st_test",
            },
        )
    )


@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    store = InMemoryDocumentStore()
    _seed(store)
    return store


@pytest.fixture
def app(doc_store: InMemoryDocumentStore) -> FastAPI:
    from app.main import create_app

    application = create_app()
    application.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    application.state.audit_object_store = StubAuditStore(locked=True)
    application.state.document_store = doc_store
    application.state.rate_limiter = RateLimiter()
    application.state.viewer_scopes = {}
    # A default allowing service + budget; individual tests override as needed.
    application.state.gemini_service = _service(_AsyncModels(list(_REPORT_CHUNKS)), _Budget())
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def parse_sse(text: str) -> list[tuple[str | None, dict[str, Any] | None]]:
    """Parse an SSE response body into ``(event, payload)`` frames."""
    frames: list[tuple[str | None, dict[str, Any] | None]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event: str | None = None
        data: str | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        frames.append((event, json.loads(data) if data else None))
    return frames


def _events(frames: list[tuple[str | None, Any]]) -> list[str | None]:
    return [e for e, _p in frames]


# ---------------------------------------------------------------------------
# SSE frame sequence: meta → delta(s) → done  (criterion 14)
# ---------------------------------------------------------------------------
class TestReportDraftStream:
    def test_meta_delta_done_sequence(self, client: TestClient) -> None:
        resp = client.post("/api/v1/studies/st_test/ai/report-draft", headers=_auth())
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = parse_sse(resp.text)
        events = _events(frames)
        assert events[0] == "meta", f"first frame must be meta, got {events}"
        assert events[-1] == "done", f"last frame must be done, got {events}"
        assert "error" not in events
        deltas = [f for f in frames if f[0] == "delta"]
        assert len(deltas) >= 1, "expected at least one delta frame"

        # meta carries the model + modelVersion from the first chunk.
        meta = frames[0][1]
        assert meta["model"] == "gemini-2.5-flash"
        assert meta["modelVersion"] == "gemini-2.5-flash-001"
        assert "requestId" in meta

        # done carries the finish reason and billable totals.
        done = frames[-1][1]
        assert done["finishReason"] == "STOP"
        assert done["totalTokens"] == 195
        assert done["cachedInputTokens"] == 5

        # Reconstructed sections are the expected two-section report.
        bodies: dict[str, str] = {}
        for section, fragment in [(d[1]["section"], d[1]["fragment"]) for d in deltas]:
            bodies[section] = bodies.get(section, "") + fragment
        assert "liver is normal" in bodies["Findings"]
        assert "No focal lesions" in bodies["Findings"]
        assert "No acute findings" in bodies["Impression"]

    def test_qa_stream_meta_delta_done(self, client: TestClient) -> None:
        # QA uses the same fake (Answer section).
        client.app.state.gemini_service = _service(
            _AsyncModels([_Chunk("<<SECTION:Answer>>The study is normal.")]), _Budget()
        )
        resp = client.post(
            "/api/v1/studies/st_test/ai/qa",
            json={"question": "Is there any acute finding?"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        frames = parse_sse(resp.text)
        events = _events(frames)
        assert events[0] == "meta"
        assert events[-1] == "done"
        assert "delta" in events
        deltas = [f for f in frames if f[0] == "delta"]
        assert any(d[1]["section"] == "Answer" for d in deltas)


# ---------------------------------------------------------------------------
# Error sequence: meta → error  (criterion 14)
# ---------------------------------------------------------------------------
class TestErrorSequence:
    def test_upstream_error_yields_meta_then_error(self, client: TestClient) -> None:
        client.app.state.gemini_service = _service(
            _AsyncModels([_Chunk("<<SECTION:Findings>>partial")], raise_exc=RuntimeError("boom")),
            _Budget(),
        )
        resp = client.post("/api/v1/studies/st_test/ai/report-draft", headers=_auth())
        assert resp.status_code == 200
        frames = parse_sse(resp.text)
        events = _events(frames)
        assert events[0] == "meta"
        assert "error" in events
        assert "done" not in events
        err = [f for f in frames if f[0] == "error"][-1][1]
        assert err["code"] == ErrorCode.AI_UNAVAILABLE.value

    def test_budget_refusal_returns_error_frame(self, client: TestClient) -> None:
        client.app.state.gemini_service = _service(
            _AsyncModels(list(_REPORT_CHUNKS)), _Budget(allowed=False)
        )
        resp = client.post("/api/v1/studies/st_test/ai/report-draft", headers=_auth())
        assert resp.status_code == 200
        frames = parse_sse(resp.text)
        events = _events(frames)
        assert events[0] == "meta"
        assert "error" in events
        assert "done" not in events
        err = [f for f in frames if f[0] == "error"][-1][1]
        assert err["code"] == ErrorCode.AI_BUDGET_EXCEEDED.value

    def test_timeout_yields_upstream_timeout_frame(self, client: TestClient) -> None:
        client.app.state.gemini_service = _service(
            _AsyncModels([_Chunk("<<SECTION:Findings>>partial")], raise_exc=TimeoutError()),
            _Budget(),
        )
        resp = client.post("/api/v1/studies/st_test/ai/report-draft", headers=_auth())
        assert resp.status_code == 200
        frames = parse_sse(resp.text)
        events = _events(frames)
        assert events[0] == "meta"
        err = [f for f in frames if f[0] == "error"][-1][1]
        assert err["code"] == ErrorCode.AI_UPSTREAM_TIMEOUT.value


# ---------------------------------------------------------------------------
# Admin → 403 PHI_ACCESS_FORBIDDEN  (criterion 18)
# ---------------------------------------------------------------------------
class TestAccessControl:
    def test_admin_gets_phi_forbidden(self, client: TestClient) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        resp = client.post("/api/v1/studies/st_test/ai/report-draft", headers=_auth())
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == ErrorCode.PHI_ACCESS_FORBIDDEN.value

    def test_admin_gets_phi_forbidden_on_qa(self, client: TestClient) -> None:
        client.app.state.token_verifier = FakeTokenVerifier(
            default_user=make_user(role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED)
        )
        resp = client.post(
            "/api/v1/studies/st_test/ai/qa",
            json={"question": "?"},
            headers=_auth(),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == ErrorCode.PHI_ACCESS_FORBIDDEN.value

    def test_missing_study_returns_404(self, client: TestClient) -> None:
        resp = client.post("/api/v1/studies/st_missing/ai/report-draft", headers=_auth())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


# ---------------------------------------------------------------------------
# Rate limit: 11th request → 429 with Retry-After  (criterion 17)
# ---------------------------------------------------------------------------
class TestRateLimit:
    def test_eleventh_request_returns_429_with_retry_after(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Lower the AI bucket to 10 so the 11th request is refused (criterion 17).
        monkeypatch.setitem(BUCKET_LIMITS, RateLimitBucket.AI, 10)
        limit = BUCKET_LIMITS[RateLimitBucket.AI]
        # First `limit` requests are allowed (200 streaming).
        for i in range(limit):
            resp = client.post("/api/v1/studies/st_test/ai/report-draft", headers=_auth())
            assert resp.status_code == 200, f"request {i + 1} should be allowed"
        # The 11th is refused with 429 + Retry-After.
        resp = client.post("/api/v1/studies/st_test/ai/report-draft", headers=_auth())
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") is not None
        assert int(resp.headers["Retry-After"]) >= 1
        assert resp.json()["error"]["code"] == ErrorCode.AI_RATE_LIMITED.value
