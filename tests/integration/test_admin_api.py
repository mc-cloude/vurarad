# ruff: noqa: B008
"""Integration tests for the admin API (WP7).

Covers user listing (NO PHI), role change (claimsVersion bump + token
revocation), disable, capability denials (radiologist → 403), and MFA
enforcement.  The token-revocation flow is exercised end-to-end: a token
minted before a role change is rejected with 401 TOKEN_REVOKED afterwards.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.services.admin_service import RawUserRecord, UserListPage
from tests.conftest import make_user


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeUserDirectory:
    """In-memory Identity Platform directory for tests."""

    def __init__(self, users: dict[str, RawUserRecord] | None = None) -> None:
        self._users: dict[str, RawUserRecord] = dict(users or {})
        self.revoked_uids: set[str] = set()
        self.revoke_calls: list[str] = []
        self.claims_updates: list[tuple[str, dict[str, Any]]] = []

    async def list_users(
        self, *, page_token: str | None, max_results: int
    ) -> UserListPage:
        users = list(self._users.values())
        return UserListPage(users=users, next_page_token=None)

    async def get_user(self, uid: str) -> RawUserRecord | None:
        return self._users.get(uid)

    async def set_custom_claims(self, uid: str, claims: dict[str, Any]) -> None:
        self.claims_updates.append((uid, dict(claims)))
        raw = self._users.get(uid)
        if raw is not None:
            raw.custom_claims = dict(claims)

    async def revoke_refresh_tokens(self, uid: str) -> None:
        self.revoke_calls.append(uid)
        self.revoked_uids.add(uid)

    async def update_user(self, uid: str, *, disabled: bool) -> None:
        raw = self._users.get(uid)
        if raw is not None:
            raw.disabled = disabled


class LinkedTokenVerifier:
    """Token verifier that revokes by uid (shared with the directory)."""

    def __init__(
        self,
        users: dict[str, Any],
        revoked_uids: set[str],
    ) -> None:
        self.users = users
        self.revoked_uids = revoked_uids

    async def verify(self, id_token: str) -> Any:
        user = self.users.get(id_token)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "TOKEN_INVALID", "message": "Unknown token"}},
            )
        if user.uid in self.revoked_uids:
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "TOKEN_REVOKED", "message": "Token has been revoked"}},
            )
        return user


def _raw(
    uid: str, email: str, role: str, operator: str, *, claims_version: int = 0
) -> RawUserRecord:
    return RawUserRecord(
        uid=uid,
        email=email,
        disabled=False,
        custom_claims={
            "role": role,
            "operatorId": operator,
            "claimsVersion": claims_version,
            "mfaEnrolled": True,
        },
        last_sign_in_at=1700000000000,
        mfa_enrolled=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def revoked_uids() -> set[str]:
    return set()


@pytest.fixture
def user_directory() -> FakeUserDirectory:
    return FakeUserDirectory(
        {
            "u-rad": _raw("u-rad", "rad@example.com", "radiologist", "op-rad", claims_version=1),
            "u-view": _raw("u-view", "viewer@example.com", "viewer", "op-view"),
            "u-admin": _raw("u-admin", "admin@example.com", "admin", "op-admin", claims_version=2),
            "victim-uid": _raw("victim-uid", "victim@example.com", "radiologist", "op-victim"),
        }
    )


@pytest.fixture
def admin_app(
    user_directory: FakeUserDirectory,
    revoked_uids: set[str],
) -> FastAPI:
    from app.main import create_app

    admin_user = make_user(
        uid="admin-uid",
        email="admin@example.com",
        role=Role.ADMIN,
        mfa_state=SecondFactorState.VERIFIED,
    )
    rad_user = make_user(
        uid="rad-uid",
        email="rad@example.com",
        role=Role.RADIOLOGIST,
        mfa_state=SecondFactorState.VERIFIED,
    )
    victim = make_user(
        uid="victim-uid",
        email="victim@example.com",
        role=Role.RADIOLOGIST,
        mfa_state=SecondFactorState.VERIFIED,
    )
    verifier = LinkedTokenVerifier(
        users={
            "admin-token": admin_user,
            "rad-token": rad_user,
            "victim-token": victim,
        },
        revoked_uids=revoked_uids,
    )
    # Link the directory's revocation set to the verifier's.
    user_directory.revoked_uids = revoked_uids

    app = create_app()
    app.state.token_verifier = verifier
    app.state.user_directory = user_directory
    return app


@pytest.fixture
def client(admin_app: FastAPI) -> TestClient:
    return TestClient(admin_app)


def _auth(token: str = "admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. GET /admin/users — NO PHI
# ---------------------------------------------------------------------------
class TestListUsers:
    def test_lists_users_with_required_fields(self, client: TestClient) -> None:
        r = client.get("/api/v1/admin/users", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert "users" in body
        uids = {u["uid"] for u in body["users"]}
        assert {"u-rad", "u-view", "u-admin"} <= uids

    def test_users_have_zero_phi_fields(self, client: TestClient) -> None:
        r = client.get("/api/v1/admin/users", headers=_auth())
        body = r.json()
        for user in body["users"]:
            # Required non-PHI fields present.
            for field in (
                "uid",
                "email",
                "operatorId",
                "role",
                "disabled",
                "mfaEnrolled",
                "lastSignInAt",
                "claimsVersion",
            ):
                assert field in user, f"missing field {field}"
            # No PHI fields.
            for phi in ("patientName", "patientBirthDate", "mrn", "patientRef"):
                assert phi not in user, f"PHI field {phi} present on AdminUser"

    def test_claims_version_reflected(self, client: TestClient) -> None:
        r = client.get("/api/v1/admin/users", headers=_auth())
        body = r.json()
        by_uid = {u["uid"]: u for u in body["users"]}
        assert by_uid["u-admin"]["claimsVersion"] == 2
        assert by_uid["u-rad"]["claimsVersion"] == 1
        assert by_uid["u-view"]["claimsVersion"] == 0


# ---------------------------------------------------------------------------
# 2. POST role change — claimsVersion bump + token revocation
# ---------------------------------------------------------------------------
class TestSetRole:
    def test_role_change_bumps_claims_version(
        self, client: TestClient, user_directory: FakeUserDirectory
    ) -> None:
        r = client.post(
            "/api/v1/admin/users/u-view/role",
            json={"role": "radiologist"},
            headers=_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "radiologist"
        assert body["claimsVersion"] == 1  # was 0 → bumped

    def test_role_change_revokes_tokens(
        self,
        client: TestClient,
        user_directory: FakeUserDirectory,
    ) -> None:
        r = client.post(
            "/api/v1/admin/users/u-view/role",
            json={"role": "radiologist"},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert "u-view" in user_directory.revoke_calls

    def test_token_minted_before_change_is_revoked(
        self,
        client: TestClient,
        user_directory: FakeUserDirectory,
    ) -> None:
        # The victim token works before the role change.
        assert client.get("/api/v1/auth/me", headers=_auth("victim-token")).status_code == 200

        # Admin changes the victim's role → tokens revoked.
        r = client.post(
            "/api/v1/admin/users/victim-uid/role",
            json={"role": "viewer"},
            headers=_auth(),
        )
        assert r.status_code == 200

        # The same (pre-change) token is now rejected.
        r2 = client.get("/api/v1/auth/me", headers=_auth("victim-token"))
        assert r2.status_code == 401
        assert r2.json()["error"]["code"] == "TOKEN_REVOKED"


# ---------------------------------------------------------------------------
# 3. POST disable
# ---------------------------------------------------------------------------
class TestDisableUser:
    def test_disable_sets_disabled_flag(
        self, client: TestClient, user_directory: FakeUserDirectory
    ) -> None:
        r = client.post(
            "/api/v1/admin/users/u-rad/disable",
            json={"disabled": True},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["disabled"] is True
        assert user_directory._users["u-rad"].disabled is True
        assert "u-rad" in user_directory.revoke_calls

    def test_reenable_clears_disabled(
        self, client: TestClient, user_directory: FakeUserDirectory
    ) -> None:
        user_directory._users["u-rad"].disabled = True
        r = client.post(
            "/api/v1/admin/users/u-rad/disable",
            json={"disabled": False},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["disabled"] is False


# ---------------------------------------------------------------------------
# 4. Capability denials — radiologist → 403 on all /admin/*
# ---------------------------------------------------------------------------
class TestAdminDenials:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/admin/users"),
            ("POST", "/api/v1/admin/users/u-view/role"),
            ("POST", "/api/v1/admin/users/u-view/disable"),
            ("DELETE", "/api/v1/admin/patients/pk-1"),
        ],
    )
    def test_radiologist_forbidden_on_admin_routes(
        self, client: TestClient, method: str, path: str
    ) -> None:
        if method == "GET":
            r = client.get(path, headers=_auth("rad-token"))
        elif method == "DELETE":
            r = client.request(
                method, path, json={"confirmPatientRef": "PT-1"}, headers=_auth("rad-token")
            )
        elif method == "POST":
            r = client.post(path, json={"role": "viewer"}, headers=_auth("rad-token"))
        else:
            r = client.request(method, path, headers=_auth("rad-token"))
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 5. MFA enforcement
# ---------------------------------------------------------------------------
class TestAdminMfa:
    def test_first_factor_only_admin_gets_mfa_required(self, admin_app: FastAPI) -> None:
        verifier = admin_app.state.token_verifier
        assert isinstance(verifier, LinkedTokenVerifier)
        verifier.users["enrolled-only"] = make_user(
            uid="admin-2", role=Role.ADMIN, mfa_state=SecondFactorState.ENROLLED
        )
        client = TestClient(admin_app)
        r = client.get("/api/v1/admin/users", headers=_auth("enrolled-only"))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"
