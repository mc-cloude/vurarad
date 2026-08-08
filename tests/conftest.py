# ruff: noqa: E402
"""Shared test fixtures for the vuraRAD WP1 test suite.

CRITICAL: ``settings = Settings()`` runs at module-import time in
``app.core.config``.  Every security-required field has no default, so the
environment variables MUST be set BEFORE the first import of anything under
``app.core``.  We do that at the very top of this file (which pytest loads
before any test module).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. Required env vars — set BEFORE importing anything from ``app.core``.
#    ``Settings()`` is constructed at module level in config.py and will raise
#    ValidationError if any required field is missing.
# ---------------------------------------------------------------------------
os.environ.setdefault("GCP_PROJECT_ID", "vurarad-test")
os.environ.setdefault("GCP_REGION", "us-central1")
os.environ.setdefault("PIXEL_BUCKET_NAME", "vurarad-test-pixels")
os.environ.setdefault("AUDIT_BUCKET_NAME", "vurarad-test-audit")
os.environ.setdefault("FIREBASE_PROJECT_ID", "vurarad-test")
# Keep the emulator OFF for unit tests so the production guard stays testable.
os.environ.pop("FIRESTORE_EMULATOR_HOST", None)

# ---------------------------------------------------------------------------
# 2. Mock service-account JSON backed by a REAL RSA keypair.
#    google.auth parses ``private_key`` as a genuine PEM key even when the
#    credentials are never used for an authenticated call — a hand-typed fake
#    PEM fails with "Could not deserialize key data".  See
#    /memory/knowledge/setup-learnings/gcp-local-dev.md.
# ---------------------------------------------------------------------------
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_private_key_pem = _rsa_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

_service_account_json = {
    "type": "service_account",
    "project_id": "vurarad-test",
    "private_key_id": "00000000-0000-0000-0000-000000000000",
    "private_key": _private_key_pem,
    "client_email": "vurarad-test@vurarad-test.iam.gserviceaccount.com",
    "client_id": "100000000000000000000",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": (
        "https://www.googleapis.com/robot/v1/metadata/x509/"
        "vurarad-test%40vurarad-test.iam.gserviceaccount.com"
    ),
    "universe_domain": "googleapis.com",
}
_cred_fd, _cred_path = tempfile.mkstemp(suffix=".json", prefix="vurarad-test-sa-")
with os.fdopen(_cred_fd, "w") as _fh:
    json.dump(_service_account_json, _fh)
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _cred_path
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "vurarad-test")

# ---------------------------------------------------------------------------
# 3. NOW it is safe to import application modules.
# ---------------------------------------------------------------------------
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, SecondFactorState
from app.core.capabilities import Role
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_user(
    *,
    uid: str = "test-uid",
    email: str | None = "test@example.com",
    role: Role = Role.VIEWER,
    mfa_state: SecondFactorState = SecondFactorState.VERIFIED,
    display_name: str | None = "Test User",
    operator_id: str = "01HZTESTOPERATOR",
) -> AuthenticatedUser:
    """Build an :class:`AuthenticatedUser` with sensible test defaults."""
    return AuthenticatedUser(
        uid=uid,
        email=email,
        role=role,
        mfa_state=mfa_state,
        display_name=display_name,
        operator_id=operator_id,
    )


def make_user_with_unknown_role(token_role: str = "ghost") -> AuthenticatedUser:
    """Construct a user whose role is NOT a :class:`Role` member.

    ``AuthenticatedUser.__post_init__`` calls ``get_role_capabilities`` which
    would raise ``KeyError`` for an invalid role, so we build a valid user and
    then bypass the frozen dataclass to inject the bogus role — exactly the
    shape a misconfigured/malicious token verifier would return.
    """
    user = make_user(role=Role.VIEWER)
    object.__setattr__(user, "role", token_role)
    return user


# ---------------------------------------------------------------------------
# FakeTokenVerifier — implements the TokenVerifier protocol for tests
# ---------------------------------------------------------------------------
class FakeTokenVerifier:
    """In-memory :class:`TokenVerifier` with configurable behaviour.

    - ``users``: per-token identity overrides.
    - ``revoked``: tokens that have been revoked → 401 TOKEN_REVOKED.
    - ``exc``: an exception to raise on every verify (e.g. for TOKEN_INVALID).
    - ``default_user``: returned for any non-revoked, unmapped token.
    """

    def __init__(
        self,
        *,
        default_user: AuthenticatedUser | None = None,
        users: dict[str, AuthenticatedUser] | None = None,
        revoked: set[str] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.default_user = default_user
        self.users: dict[str, AuthenticatedUser] = users or {}
        self.revoked: set[str] = revoked or set()
        self.exc = exc
        self.verify_calls: list[str] = []

    async def verify(self, id_token: str) -> AuthenticatedUser:
        self.verify_calls.append(id_token)
        if self.exc is not None:
            raise self.exc
        if id_token in self.revoked:
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "TOKEN_REVOKED", "message": "Token has been revoked"}},
            )
        if id_token in self.users:
            return self.users[id_token]
        if self.default_user is not None:
            return self.default_user
        return make_user()


# A sentinel token used across tests.
VALID_TOKEN = "valid-test-token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_verifier() -> FakeTokenVerifier:
    return FakeTokenVerifier()


@pytest.fixture
def app(fake_verifier: FakeTokenVerifier) -> FastAPI:
    application = create_app()
    application.state.token_verifier = fake_verifier
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def reset_root_logging() -> Any:
    """Snapshot/restore the root logger so repeated ``create_app`` calls
    (each of which calls ``setup_logging``) do not bleed handlers into other
    tests."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_filters = root.filters[:]
    saved_level = root.level
    yield root
    root.handlers[:] = saved_handlers
    root.filters[:] = saved_filters
    root.setLevel(saved_level)
