"""Verify no HMAC or keyed hashing is used anywhere under app/.

Report content hashes must be plain SHA-256 (``hashlib.sha256``), never HMAC.
This AST scan walks every ``.py`` file under ``app/`` and asserts:

- No ``import hmac`` or ``from hmac import ...``.
- No ``hashlib.new(...)`` call with a ``key`` keyword argument (HMAC mode).

The ``ReportDraft.compute_content_hash`` method is also inspected directly to
confirm it uses ``hashlib.sha256`` on canonical JSON — the property frozen into
:class:`ReportSignature` at sign time.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path


def _app_root() -> Path:
    """Return the path to the ``app/`` package directory."""
    return Path(__file__).resolve().parents[2] / "app"


def _python_files(root: Path) -> list[Path]:
    """Collect every ``.py`` file under ``root``."""
    return sorted(root.rglob("*.py"))


def _hmac_imports(tree: ast.AST) -> list[str]:
    """Return descriptions of any ``import hmac`` / ``from hmac import`` found."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "hmac" or alias.name.startswith("hmac."):
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "hmac" or node.module.startswith("hmac.")
        ):
            hits.append(f"from {node.module} import ...")
    return hits


def _keyed_hashlib_new_calls(tree: ast.AST) -> list[str]:
    """Return descriptions of any ``hashlib.new(...)`` call with a ``key`` kwarg."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "new"
                and isinstance(func.value, ast.Name)
                and func.value.id == "hashlib"
            ):
                for kw in node.keywords:
                    if kw.arg == "key":
                        hits.append("hashlib.new(..., key=...) — keyed hash (HMAC)")
    return hits


def test_no_hmac_import_anywhere_in_app() -> None:
    """No file under app/ may import the ``hmac`` module."""
    root = _app_root()
    assert root.is_dir(), f"app/ directory not found at {root}"
    violations: list[str] = []
    for path in _python_files(root):
        tree = ast.parse(path.read_text(), filename=str(path))
        for hit in _hmac_imports(tree):
            violations.append(f"{path}: {hit}")
    assert not violations, "HMAC import found:\n" + "\n".join(violations)


def test_no_keyed_hashlib_new_anywhere_in_app() -> None:
    """No file under app/ may call ``hashlib.new`` with a ``key`` argument."""
    root = _app_root()
    violations: list[str] = []
    for path in _python_files(root):
        tree = ast.parse(path.read_text(), filename=str(path))
        for hit in _keyed_hashlib_new_calls(tree):
            violations.append(f"{path}: {hit}")
    assert not violations, "Keyed hashlib.new found:\n" + "\n".join(violations)


def test_report_content_hash_uses_plain_sha256() -> None:
    """ReportDraft.compute_content_hash must use hashlib.sha256, not hmac."""
    from app.models.report import ReportDraft

    source = inspect.getsource(ReportDraft.compute_content_hash)
    assert "hashlib.sha256" in source
    # The docstring says "NOT HMAC" so we check for actual hmac *usage*
    # (attribute access or call) rather than the bare substring.
    assert "hmac." not in source
    assert "import hmac" not in source
