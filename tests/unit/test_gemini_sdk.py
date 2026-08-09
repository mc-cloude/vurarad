# ruff: noqa: E402
"""Gemini SDK integration invariants — Vertex-ADC-only, no legacy aiplatform.

Asserts the security posture of :class:`GeminiService`:

- importing ``app.main`` does NOT pull the legacy ``vertexai`` package into
  ``sys.modules`` (criterion 2);
- no ``app/`` source references ``vertexai.generative_models`` (the legacy
  google-cloud-aiplatform module path);
- :class:`GeminiService` refuses to initialise when any of the four banned
  environment variables is set (criterion 3) — four separate tests.
"""

from __future__ import annotations

import ast
import sys

import pytest

from app.services.gemini_service import GeminiService
from tests.conftest import REPO_ROOT


def test_vertexai_not_in_sys_modules() -> None:
    """Criterion 2: "vertexai" absent from sys.modules after importing app.main."""
    import app.main  # noqa: F401  — imported for its side effects

    assert "vertexai" not in sys.modules, (
        "The legacy 'vertexai' package must not be imported; the google-genai "
        "SDK with vertexai=True uses ADC over REST, not google-cloud-aiplatform."
    )


def test_no_vertexai_generative_models_string() -> None:
    """No ``app/`` source references the legacy ``vertexai.generative_models`` module."""
    offenders: list[str] = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        text = path.read_text()
        if "vertexai.generative_models" in text:
            offenders.append(str(path))
    assert offenders == [], "vertexai.generative_models found in: " + ", ".join(offenders)


def test_streaming_path_never_reads_chunk_parsed() -> None:
    """Criterion 7: the streaming path reads ``chunk.text``, never ``chunk.parsed``.

    Uses the AST so docstring mentions of ``chunk.parsed`` do not count — only
    real attribute accesses (``.parsed``) are flagged.
    """
    source = (REPO_ROOT / "app" / "services" / "gemini_service.py").read_text()
    tree = ast.parse(source)
    parsed_accesses = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "parsed"
    ]
    assert parsed_accesses == [], (
        "gemini_service.py must never access .parsed on the streaming path; "
        f"found {len(parsed_accesses)} access(es)"
    )


@pytest.mark.parametrize(
    ("env_var"),
    [
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GENAI_USE_ENTERPRISE",
    ],
)
def test_gemini_service_raises_on_banned_env(env_var: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """GeminiService refuses to initialise when a banned env var is set (criterion 3)."""
    monkeypatch.setenv(env_var, "present")
    with pytest.raises(RuntimeError, match=env_var):
        GeminiService(project="vurarad-test", location="us-central1")


def test_gemini_service_raises_on_google_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 3: GOOGLE_API_KEY set → RuntimeError (API-key path forbidden)."""
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-fake-key")
    with pytest.raises(RuntimeError):
        GeminiService(project="vurarad-test", location="us-central1")


def test_gemini_service_raises_on_gemini_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 3: GEMINI_API_KEY set → RuntimeError."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with pytest.raises(RuntimeError):
        GeminiService(project="vurarad-test", location="us-central1")


def test_gemini_service_raises_on_use_vertexai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 3: GOOGLE_GENAI_USE_VERTEXAI set → RuntimeError (override forbidden)."""
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    with pytest.raises(RuntimeError):
        GeminiService(project="vurarad-test", location="us-central1")


def test_gemini_service_raises_on_use_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 3: GOOGLE_GENAI_USE_ENTERPRISE set → RuntimeError."""
    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "true")
    with pytest.raises(RuntimeError):
        GeminiService(project="vurarad-test", location="us-central1")
