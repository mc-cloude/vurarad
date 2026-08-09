"""Gemini service — Vertex AI (ADC) streaming with budget control and accounting.

Security posture (enforced by ``test_gemini_sdk.py``):

- **Vertex AI with Application Default Credentials only — never an API key.**
  The constructor refuses to initialise if any of ``GOOGLE_API_KEY``,
  ``GEMINI_API_KEY``, ``GOOGLE_GENAI_USE_VERTEXAI``, or
  ``GOOGLE_GENAI_USE_ENTERPRISE`` is present in the environment.  The first two
  would select the Gemini API key path; the latter two would override the
  explicit ``vertexai=True`` we pass to the client.
- **Thinking is disabled on every call** —
  ``thinking_config=types.ThinkingConfig(thinking_budget=0)`` — so the model
  never spends tokens on hidden reasoning we cannot attribute or audit.
- **Budget is checked before any SDK call** (criterion 12); a refusal is
  delivered as an SSE ``error`` frame (``AI_BUDGET_EXCEEDED``) with no SDK call.
- **Token accounting runs in a ``finally`` block** (criterion 11) so a
  mid-stream abort still books its consumed tokens.  Every count is recorded
  with ``None → 0``; ``thoughts_token_count`` is tracked and billed.
- **The streaming path never reads ``chunk.parsed``** — it feeds ``chunk.text``
  to :class:`SectionStreamParser`.  ``chunk.parsed`` is the SDK's structured
  output accessor and is deliberately avoided so we control parsing.
- **A 30 s SDK timeout** is set via ``http_options=types.HttpOptions(timeout=30_000)``;
  a timeout surfaces as an ``error`` frame with ``AI_UPSTREAM_TIMEOUT``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types

from app.models.ai import AiStreamDelta, AiStreamDone, AiStreamError, AiStreamMeta
from app.models.errors import ErrorCode
from app.repositories.ai_budget_repo import AiBudgetRepo
from app.services.gemini_stream import SectionStreamParser

logger = logging.getLogger("vurarad.ai")

# ---------------------------------------------------------------------------
# Environment variables that would select a non-Vertex path or override our
# explicit vertexai=True.  Their presence is a misconfiguration → RuntimeError.
# ---------------------------------------------------------------------------
_BANNED_ENV: tuple[str, ...] = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_ENTERPRISE",
)

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_HTTP_TIMEOUT_MS = 30_000

# ---------------------------------------------------------------------------
# FinishReason → wire string.  This is an *explicit* mapping over the members
# known at the time of writing.  ``test_finish_reason_map.py`` iterates every
# ``types.FinishReason`` member from the *installed* SDK and asserts each is
# present here — so an SDK upgrade that adds a new member fails the build until
# someone explicitly maps it (criterion 9).
# ---------------------------------------------------------------------------
_KNOWN_FINISH_REASONS: tuple[types.FinishReason, ...] = (
    types.FinishReason.FINISH_REASON_UNSPECIFIED,
    types.FinishReason.STOP,
    types.FinishReason.MAX_TOKENS,
    types.FinishReason.SAFETY,
    types.FinishReason.RECITATION,
    types.FinishReason.LANGUAGE,
    types.FinishReason.OTHER,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.SPII,
    types.FinishReason.MALFORMED_FUNCTION_CALL,
    types.FinishReason.IMAGE_SAFETY,
    types.FinishReason.UNEXPECTED_TOOL_CALL,
    types.FinishReason.IMAGE_PROHIBITED_CONTENT,
    types.FinishReason.NO_IMAGE,
    types.FinishReason.IMAGE_RECITATION,
    types.FinishReason.IMAGE_OTHER,
)

FINISH_REASON_MAP: dict[types.FinishReason, str] = {
    reason: reason.value for reason in _KNOWN_FINISH_REASONS
}


def map_finish_reason(reason: types.FinishReason | None) -> str | None:
    """Map a SDK :class:`FinishReason` to its wire string (``None`` → ``None``)."""
    if reason is None:
        return None
    return FINISH_REASON_MAP.get(reason, reason.value)


# ---------------------------------------------------------------------------
# Token accounting — None → 0, thoughts included
# ---------------------------------------------------------------------------
_USAGE_FIELDS: tuple[str, ...] = (
    "prompt_token_count",
    "candidates_token_count",
    "thoughts_token_count",
    "cached_content_token_count",
    "total_token_count",
)


def _zero_usage() -> dict[str, int]:
    return dict.fromkeys(_USAGE_FIELDS, 0)


def _extract_usage(metadata: Any) -> dict[str, int]:
    """Read the five token counts from a usage-metadata object, ``None → 0``."""
    return {field: int(getattr(metadata, field, None) or 0) for field in _USAGE_FIELDS}


def _is_timeout(exc: BaseException) -> bool:
    """Classify an exception as an upstream timeout (SDK / httpx / asyncio)."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    return "timeout" in type(exc).__name__.lower()


class GeminiService:
    """Vertex AI streaming client — ADC-only, thinking-disabled, budget-gated."""

    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str = DEFAULT_MODEL,
        client: genai.Client | None = None,
        budget_repo: AiBudgetRepo | None = None,
        http_timeout_ms: int = DEFAULT_HTTP_TIMEOUT_MS,
    ) -> None:
        # Refuse to start if the environment would select a non-Vertex path.
        present = [var for var in _BANNED_ENV if var in os.environ]
        if present:
            raise RuntimeError(
                "GeminiService requires Vertex AI with ADC only; refusing to "
                f"initialise with banned env var(s) set: {present}"
            )

        self._model = model
        self._budget = budget_repo
        self._client = client or genai.Client(
            vertexai=True,
            project=project,
            location=location,
            http_options=types.HttpOptions(timeout=http_timeout_ms),
        )

    # -- public API ----------------------------------------------------------
    def generate_stream(
        self,
        *,
        request_id: str,
        contents: str,
        system_instruction: str | None = None,
        tenant_id: str = "default",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield SSE frames: ``meta → delta(s) → done`` or ``meta → error``.

        - Budget is checked **before** any SDK call (criterion 12).
        - Token accounting runs in a ``finally`` block (criterion 11).
        - The streaming path reads ``chunk.text`` only, never ``chunk.parsed``.
        - A timeout surfaces as ``meta → error`` with ``AI_UPSTREAM_TIMEOUT``.
        """
        return self._generate_stream(
            request_id=request_id,
            model=model or self._model,
            contents=contents,
            system_instruction=system_instruction,
            tenant_id=tenant_id,
        )

    async def _generate_stream(
        self,
        *,
        request_id: str,
        model: str,
        contents: str,
        system_instruction: str | None,
        tenant_id: str,
    ) -> AsyncIterator[str]:
        meta_sent = False
        usage = _zero_usage()
        finish_reason: types.FinishReason | None = None
        parser = SectionStreamParser()

        def meta_frame(model_version: str | None) -> str:
            return _sse(
                "meta",
                AiStreamMeta(
                    request_id=request_id,
                    model=model,
                    model_version=model_version,
                ).model_dump(by_alias=True),
            )

        try:
            # -- budget gate: BEFORE any SDK call (criterion 12) --------------
            if self._budget is not None and not await self._budget.check_budget(tenant_id):
                if not meta_sent:
                    yield meta_frame(None)
                    meta_sent = True
                yield _sse(
                    "error",
                    AiStreamError(
                        code=ErrorCode.AI_BUDGET_EXCEEDED.value,
                        message="AI monthly budget exceeded",
                    ).model_dump(by_alias=True),
                )
                return

            # -- build config: thinking disabled on every call ----------------
            config = types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                system_instruction=system_instruction,
            )

            stream = await self._client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )

            async for chunk in stream:
                # First chunk carries the model version — emit meta once.
                if not meta_sent:
                    yield meta_frame(getattr(chunk, "model_version", None))
                    meta_sent = True

                # Token accounting (cumulative on the last chunk; None → 0).
                metadata = getattr(chunk, "usage_metadata", None)
                if metadata is not None:
                    usage = _extract_usage(metadata)

                # Finish reason (on the final chunk).
                candidates = getattr(chunk, "candidates", None)
                if candidates:
                    reason = getattr(candidates[0], "finish_reason", None)
                    if reason is not None:
                        finish_reason = reason

                # Streamed text → section deltas.  chunk.text only, never parsed.
                text = chunk.text
                if text:
                    for section, fragment in parser.feed(text):
                        yield _sse(
                            "delta",
                            AiStreamDelta(
                                section=section, fragment=fragment
                            ).model_dump(by_alias=True),
                        )

            # Flush any body held back as a potential partial marker.
            for section, fragment in parser.flush():
                yield _sse(
                    "delta",
                    AiStreamDelta(section=section, fragment=fragment).model_dump(by_alias=True),
                )

            if not meta_sent:
                yield meta_frame(None)
                meta_sent = True

            yield _sse(
                "done",
                AiStreamDone(
                    finish_reason=map_finish_reason(finish_reason),
                    total_tokens=usage["total_token_count"],
                    cached_input_tokens=usage["cached_content_token_count"],
                ).model_dump(by_alias=True),
            )

        except Exception as exc:  # noqa: BLE001 — surface every failure as a frame
            if not meta_sent:
                yield meta_frame(None)
                meta_sent = True
            if _is_timeout(exc):
                code = ErrorCode.AI_UPSTREAM_TIMEOUT.value
                message = "AI upstream request timed out"
            else:
                code = ErrorCode.AI_UNAVAILABLE.value
                message = "AI upstream request failed"
            logger.warning("AI_STREAM_ERROR", extra={"code": code, "error": repr(exc)})
            yield _sse(
                "error",
                AiStreamError(code=code, message=message).model_dump(by_alias=True),
            )
        finally:
            # Accounting runs even on abort (criterion 11).
            if self._budget is not None:
                try:
                    await self._budget.record_usage(tenant_id, dict(usage))
                except Exception:  # noqa: BLE001 — never mask the stream error
                    logger.warning("AI_BUDGET_RECORD_FAILED", exc_info=True)


def _sse(event: str, payload: dict[str, Any]) -> str:
    """Format one SSE frame: ``event: <e>\\ndata: <json>\\n\\n``."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


__all__ = [
    "DEFAULT_HTTP_TIMEOUT_MS",
    "DEFAULT_MODEL",
    "FINISH_REASON_MAP",
    "GeminiService",
    "map_finish_reason",
]
