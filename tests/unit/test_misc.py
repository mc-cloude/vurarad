"""Coverage for the remaining foundation modules + source-level invariants.

Covers: deps, security headers, idempotency, common wire models, analytics,
and the static-source acceptance criteria (#11 forbidden tokens, #12 auth.py
has no ``except ImportError`` / ``os.getenv`` reachable from get_current_user).
"""

from __future__ import annotations

import ast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.capabilities import Role
from app.core.config import settings
from app.core.deps import get_settings
from app.core.idempotency import IdempotencyStore
from app.models.audit import AuditEvent
from app.models.common import CamelModel, CursorPage
from app.services.analytics_service import AnalyticsCounterStore, AnalyticsService
from tests.conftest import REPO_ROOT, VALID_TOKEN, FakeTokenVerifier, make_user


# ---------------------------------------------------------------------------
# deps.get_settings
# ---------------------------------------------------------------------------
async def test_get_settings_returns_app_state_settings() -> None:
    request = MagicMock()
    request.app.state.settings = settings
    assert await get_settings(request) is settings


# ---------------------------------------------------------------------------
# Security headers (every response)
# ---------------------------------------------------------------------------
def test_security_headers_present(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "referrer-policy" in r.headers
    assert "permissions-policy" in r.headers
    assert "content-security-policy" in r.headers


# ---------------------------------------------------------------------------
# IdempotencyStore
# ---------------------------------------------------------------------------
def test_idempotency_new_record() -> None:
    store = IdempotencyStore()
    is_new, reason = store.check_or_record("u1", "key-1", b"body", "{}", 200)
    assert is_new is True
    assert reason == ""


def test_idempotency_replay_same_body_returns_ok() -> None:
    store = IdempotencyStore()
    store.check_or_record("u1", "key-1", b"body", "{}", 200)
    is_new, reason = store.check_or_record("u1", "key-1", b"body", "{}", 200)
    # Replay with identical body is accepted (is_new=True, no conflict).
    assert is_new is True
    assert reason == ""


def test_idempotency_mismatch_returns_conflict() -> None:
    store = IdempotencyStore()
    store.check_or_record("u1", "key-1", b"body-A", "{}", 200)
    is_new, reason = store.check_or_record("u1", "key-1", b"body-B", "{}", 200)
    assert is_new is False
    assert reason == "IDEMPOTENCY_MISMATCH"


def test_idempotency_different_users_same_key_independent() -> None:
    store = IdempotencyStore()
    store.check_or_record("u1", "key-1", b"body", "{}", 200)
    is_new, reason = store.check_or_record("u2", "key-1", b"body", "{}", 200)
    assert is_new is True
    assert reason == ""


def test_idempotency_expired_key_allows_reuse() -> None:
    store = IdempotencyStore()
    is_new, _ = store.check_or_record("u1", "key-1", b"body", "{}", 200)
    assert is_new is True
    # Back-date the stored record so it is already expired (past the 24 h TTL).
    record = store._records["u1:key-1"]
    record.created_at = 0.0
    record.expire_at = 1.0
    # An expired key with a different body must allow reuse (not 409 mismatch).
    is_new, reason = store.check_or_record("u1", "key-1", b"body-new", "{}", 200)
    assert is_new is True
    assert reason == ""


# ---------------------------------------------------------------------------
# Common wire models
# ---------------------------------------------------------------------------
def test_camel_model_alias_round_trip() -> None:
    class Sample(CamelModel):
        study_id: str
        patient_key: str | None = None

    obj = Sample.model_validate({"studyId": "S1", "patientKey": "k"})
    assert obj.study_id == "S1"
    assert obj.patient_key == "k"
    dumped = obj.model_dump(by_alias=True)
    assert dumped["studyId"] == "S1"
    assert dumped["patientKey"] == "k"


def test_camel_model_forbids_extra() -> None:
    class Sample(CamelModel):
        study_id: str

    with pytest.raises(ValidationError):
        Sample.model_validate({"studyId": "S1", "extra": "nope"})


def test_cursor_page_shape() -> None:
    page = CursorPage[dict[str, str]](items=[{"a": "b"}], next_cursor="c", total=1)
    dumped = page.model_dump(by_alias=True)
    assert dumped["items"] == [{"a": "b"}]
    assert dumped["nextCursor"] == "c"
    assert dumped["total"] == 1


# ---------------------------------------------------------------------------
# AnalyticsService
# ---------------------------------------------------------------------------
class FakeCounterStore:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    async def increment(self, counter_name: str, amount: int = 1) -> int:
        self.counters[counter_name] = self.counters.get(counter_name, 0) + amount
        return self.counters[counter_name]

    async def read(self, counter_name: str) -> int:
        return self.counters.get(counter_name, 0)


async def test_analytics_counters() -> None:
    service = AnalyticsService(FakeCounterStore())
    assert await service.study_accessed() == 1
    assert await service.report_created() == 1
    assert await service.report_signed() == 1
    assert await service.ai_request() == 1
    assert await service.images_ingested(5) == 5
    assert await service.images_viewed(3) == 3
    assert await service.audit_chain_length() == 0
    assert await service.increment_audit_chain_length() == 1


def test_fake_counter_store_satisfies_protocol() -> None:
    store: AnalyticsCounterStore = FakeCounterStore()
    assert hasattr(store, "increment")
    assert hasattr(store, "read")


# ---------------------------------------------------------------------------
# Static-source invariants
# ---------------------------------------------------------------------------
FORBIDDEN_TOKENS = ("DEV_TIER", "dev-user", "master-vura-v5", "vertexai.generative_models")


def test_no_forbidden_tokens_in_app_source() -> None:
    """Criterion #11: none of the legacy/secret tokens appear in app/."""
    offenders: list[str] = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        text = path.read_text()
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path}: {token}")
    assert offenders == [], "forbidden tokens found: " + ", ".join(offenders)


def _function_nodes(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_auth_no_except_importerror_reachable_from_get_current_user() -> None:
    """Criterion #12: no ``except ImportError`` in any auth function."""
    source = (REPO_ROOT / "app" / "core" / "auth.py").read_text()
    tree = ast.parse(source)
    for fn in _function_nodes(tree):
        for node in ast.walk(fn):
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                assert ast.unparse(node.type) != "ImportError", (
                    f"except ImportError found in {fn.name}"
                )


def test_auth_no_os_getenv_reachable_from_get_current_user() -> None:
    """Criterion #12: no ``os.getenv`` call in any auth function."""
    source = (REPO_ROOT / "app" / "core" / "auth.py").read_text()
    tree = ast.parse(source)
    for fn in _function_nodes(tree):
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                raise AssertionError(f"os.getenv found in {fn.name}")


def test_auth_me_returns_role_capabilities_for_admin(
    fake_verifier: FakeTokenVerifier, client: TestClient
) -> None:
    """Admin capabilities must be the administrative set (no PHI)."""
    fake_verifier.users[VALID_TOKEN] = make_user(
        uid="admin-1", email="admin@example.com", role=Role.ADMIN
    )
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    caps = set(r.json()["capabilities"])
    assert caps == {"audit:read", "audit:export", "analytics:read", "user:manage"}


# ---------------------------------------------------------------------------
# AuditEvent default timestamp (covers the default_factory line)
# ---------------------------------------------------------------------------
def test_audit_event_default_timestamp() -> None:
    e = AuditEvent(seq=1, prev_hash="0" * 64, event_type="X", actor="u", second_factor=True)
    assert e.timestamp > 0
    assert e.detail == {}
    assert e.hash == ""  # not sealed yet
