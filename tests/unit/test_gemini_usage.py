"""GeminiService token accounting, budget gating, and thinking config (criteria 5,10,11,12).

Uses in-memory fakes for the genai SDK — never the real API.  Covers:

- ``thoughts_token_count`` is captured and billed (criterion 10);
- ``None`` token counts are treated as ``0`` (criterion 10);
- the ``finally`` block records consumed tokens even on a mid-stream abort
  (criterion 11);
- the budget is checked **before** any SDK call (criterion 12);
- ``thinking_budget=0`` is set on every call (criterion 5).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from google.genai import types

from app.services.gemini_service import GeminiService


# ---------------------------------------------------------------------------
# Fakes for the genai SDK — never call the real API
# ---------------------------------------------------------------------------
class FakeUsage:
    def __init__(
        self,
        *,
        prompt: int | None = None,
        candidates: int | None = None,
        thoughts: int | None = None,
        cached: int | None = None,
        total: int | None = None,
    ) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts
        self.cached_content_token_count = cached
        self.total_token_count = total


class FakeCandidate:
    def __init__(self, finish_reason: types.FinishReason | None = None) -> None:
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(
        self,
        *,
        text: str | None = None,
        usage: FakeUsage | None = None,
        finish_reason: types.FinishReason | None = None,
        model_version: str = "fake-model-v1",
    ) -> None:
        self.text = text
        self.usage_metadata = usage
        self.candidates = [FakeCandidate(finish_reason)] if finish_reason is not None else None
        self.model_version = model_version


class FakeAsyncModels:
    def __init__(self, chunks: list[FakeChunk], raise_exc: BaseException | None = None) -> None:
        self._chunks = chunks
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def generate_content_stream(
        self,
        *,
        model: str,
        contents: str,
        config: Any = None,
    ) -> AsyncIterator[FakeChunk]:
        # The real SDK's generate_content_stream is an `async def` that RETURNS
        # an async iterator (so callers do `await ...generate_content_stream()`
        # then `async for`).  Mirror that: record the call, return an iterator.
        self.calls.append({"model": model, "contents": contents, "config": config})

        async def _iterator() -> AsyncIterator[FakeChunk]:
            for chunk in self._chunks:
                yield chunk
            if self._raise is not None:
                raise self._raise

        return _iterator()


class FakeAio:
    def __init__(self, models: FakeAsyncModels) -> None:
        self.models = models


class FakeClient:
    def __init__(self, models: FakeAsyncModels) -> None:
        self.aio = FakeAio(models)


class FakeBudgetRepo:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.check_calls = 0
        self.recorded: list[tuple[str, dict[str, int]]] = []

    async def check_budget(self, tenant_id: str) -> bool:
        self.check_calls += 1
        return self.allowed

    async def record_usage(self, tenant_id: str, usage: dict[str, int]) -> None:
        self.recorded.append((tenant_id, dict(usage)))


def _make_service(
    models: FakeAsyncModels,
    budget: FakeBudgetRepo | None = None,
) -> GeminiService:
    return GeminiService(
        project="vurarad-test",
        location="us-central1",
        client=FakeClient(models),
        budget_repo=budget,
    )


async def _collect(stream: Any) -> list[tuple[str | None, dict[str, Any] | None]]:
    """Consume an SSE async iterator into a list of (event, payload) pairs."""
    frames: list[tuple[str | None, dict[str, Any] | None]] = []
    async for raw in stream:
        event: str | None = None
        data: str | None = None
        for line in raw.strip().split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        frames.append((event, json.loads(data) if data else None))
    return frames


# ---------------------------------------------------------------------------
# Criterion 10 — thoughts_token_count is captured and billed
# ---------------------------------------------------------------------------
async def test_thoughts_token_count_billed() -> None:
    models = FakeAsyncModels(
        [
            FakeChunk(text="<<SECTION:Findings>>Liver normal.", model_version="mv-9"),
            FakeChunk(
                text="",
                usage=FakeUsage(prompt=100, candidates=50, thoughts=40, cached=10, total=200),
                finish_reason=types.FinishReason.STOP,
            ),
        ]
    )
    budget = FakeBudgetRepo()
    service = _make_service(models, budget)

    frames = await _collect(
        service.generate_stream(
            request_id="req-1",
            contents="prompt",
            system_instruction="sys",
            tenant_id="tenant-a",
        )
    )

    # The recorded usage includes thoughts_token_count.
    assert budget.recorded, "record_usage must be called"
    _tenant, usage = budget.recorded[0]
    assert usage["thoughts_token_count"] == 40
    assert usage["total_token_count"] == 200
    assert usage["prompt_token_count"] == 100
    assert usage["cached_content_token_count"] == 10

    # The done frame carries the billable total.
    done = [f for f in frames if f[0] == "done"]
    assert done and done[-1][1]["totalTokens"] == 200


# ---------------------------------------------------------------------------
# Criterion 10 — None token counts are treated as 0
# ---------------------------------------------------------------------------
async def test_none_token_counts() -> None:
    models = FakeAsyncModels(
        [
            FakeChunk(text="<<SECTION:Impression>>ok"),
            FakeChunk(
                text=None,
                usage=FakeUsage(),  # all fields None
                finish_reason=types.FinishReason.STOP,
            ),
        ]
    )
    budget = FakeBudgetRepo()
    service = _make_service(models, budget)

    await _collect(
        service.generate_stream(request_id="r", contents="c", tenant_id="t")
    )

    _tenant, usage = budget.recorded[0]
    for field in (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "cached_content_token_count",
        "total_token_count",
    ):
        assert usage[field] == 0, f"{field} should be 0 for None, got {usage[field]}"

    # None text chunks are skipped by the parser (no spurious deltas).
    models2 = FakeAsyncModels(
        [FakeChunk(text=None, usage=FakeUsage(), finish_reason=types.FinishReason.STOP)]
    )
    service2 = _make_service(models2, FakeBudgetRepo())
    frames = await _collect(service2.generate_stream(request_id="r2", contents="c", tenant_id="t"))
    deltas = [f for f in frames if f[0] == "delta"]
    assert deltas == []


# ---------------------------------------------------------------------------
# Criterion 11 — the finally block records tokens even on an aborted stream
# ---------------------------------------------------------------------------
async def test_finally_block_records_tokens() -> None:
    models = FakeAsyncModels(
        [
            FakeChunk(
                text="<<SECTION:Findings>>partial body",
                usage=FakeUsage(prompt=80, candidates=40, thoughts=20, cached=0, total=140),
            ),
            # The second "chunk" raises — mid-stream abort.
        ],
        raise_exc=RuntimeError("upstream blew up"),
    )
    budget = FakeBudgetRepo()
    service = _make_service(models, budget)

    frames = await _collect(
        service.generate_stream(request_id="r", contents="c", tenant_id="tenant-x")
    )

    # Tokens consumed before the abort were still recorded.
    assert budget.recorded, "record_usage must run in finally even on abort"
    _tenant, usage = budget.recorded[0]
    assert _tenant == "tenant-x"
    assert usage["total_token_count"] == 140
    assert usage["thoughts_token_count"] == 20

    # An error frame was emitted (the abort surfaces as AI_UNAVAILABLE).
    errors = [f for f in frames if f[0] == "error"]
    assert errors and errors[-1][1]["code"] == "AI_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Criterion 12 — budget is checked BEFORE any SDK call
# ---------------------------------------------------------------------------
async def test_budget_exceeded_before_call() -> None:
    models = FakeAsyncModels(
        [FakeChunk(text="<<SECTION:Findings>>x", finish_reason=types.FinishReason.STOP)]
    )
    budget = FakeBudgetRepo(allowed=False)
    service = _make_service(models, budget)

    frames = await _collect(
        service.generate_stream(request_id="r", contents="c", tenant_id="tenant-broke")
    )

    # No SDK call was made — the budget gate refused first.
    assert models.calls == [], "generate_content_stream must not be called when budget exceeded"

    # Frame sequence is meta → error with AI_BUDGET_EXCEEDED.
    events = [e for e, _p in frames]
    assert events[0] == "meta"
    assert "error" in events
    err = [f for f in frames if f[0] == "error"][-1][1]
    assert err["code"] == "AI_BUDGET_EXCEEDED"
    # No done frame on a budget refusal.
    assert "done" not in events

    # The budget was checked exactly once.
    assert budget.check_calls == 1


# ---------------------------------------------------------------------------
# Criterion 5 — thinking_budget=0 on every call
# ---------------------------------------------------------------------------
async def test_thinking_budget_zero_on_every_call() -> None:
    models = FakeAsyncModels(
        [FakeChunk(text="<<SECTION:Findings>>x", finish_reason=types.FinishReason.STOP)]
    )
    service = _make_service(models, FakeBudgetRepo())

    await _collect(service.generate_stream(request_id="r", contents="c", tenant_id="t"))

    assert len(models.calls) == 1
    config = models.calls[0]["config"]
    assert config is not None
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0


# ---------------------------------------------------------------------------
# Criterion 16 — a timeout surfaces as an error frame with AI_UPSTREAM_TIMEOUT
# ---------------------------------------------------------------------------
async def test_timeout_yields_upstream_timeout_frame() -> None:
    models = FakeAsyncModels(
        [FakeChunk(text="<<SECTION:Findings>>x")],
        raise_exc=TimeoutError(),
    )
    service = _make_service(models, FakeBudgetRepo())

    frames = await _collect(service.generate_stream(request_id="r", contents="c", tenant_id="t"))

    err = [f for f in frames if f[0] == "error"][-1][1]
    assert err["code"] == "AI_UPSTREAM_TIMEOUT"
