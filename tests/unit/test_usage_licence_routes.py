"""WP14 route wiring — auth gating, billing:manage + fresh 2FA (criterion 8).

``billing:manage`` routes require the capability AND a fresh second factor.
``usage:read`` gates the self-service usage routes.  These are route-level
checks; the service logic is covered by the dedicated unit-test modules.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.core.config import SpendStage
from app.services.ceiling_service import CeilingState
from app.services.licence_service import LicenceClaims, LicenceState
from tests.conftest import VALID_TOKEN, make_user


# ---------------------------------------------------------------------------
# Fake services — duck-typed to what the routes call
# ---------------------------------------------------------------------------
class _FakeStore:
    def __init__(self) -> None:
        self.ceiling_calls: list[tuple[str, float | None, str, bool]] = []

    async def set_ceiling_config(
        self, tenant_id: str, ceiling_usd: float | None, reason: str, suspended: bool
    ) -> None:
        self.ceiling_calls.append((tenant_id, ceiling_usd, reason, suspended))

    async def get_usage(self, tenant_id: str, period: str) -> None:
        return None

    async def list_history(self, tenant_id: str, months: int) -> list[object]:
        return []

    async def increment_meter(self, *args: object, **kwargs: object) -> None:
        pass

    async def record_event(self, *args: object, **kwargs: object) -> None:
        pass

    async def get_ceiling_config(self, tenant_id: str) -> None:
        return None


class _FakeCeilingService:
    async def compute_state(
        self, tenant_id: str, *, period: str | None = None, now: object = None
    ) -> CeilingState:
        return CeilingState(
            tenant_id=tenant_id,
            period=period or "2026-08",
            stage=SpendStage.OK,
            infra_cost_usd=1.0,
            ceiling_usd=10.0,
            ratio=0.1,
            images_included=1000,
            images_used=0,
            overage_images=0,
            overage_charge_usd=0.0,
            projected_month_end_usd=3.0,
        )


class _FakeLicenceService:
    async def install(self, token: str) -> LicenceClaims:
        return LicenceClaims(
            site_id="site-1",
            seats=5,
            tier="P2",
            features=["workbench"],
            not_before=0,
            not_after=2_000_000_000,
        )

    async def current_state(self, *, now: int | None = None) -> LicenceState:
        return LicenceState(valid=True, site_id="site-1", tier="P2", seats=5)


@pytest.fixture
def wp14_app(
    app: FastAPI,
    fake_verifier: object,  # noqa: ARG001
) -> FastAPI:
    app.state.usage_store = _FakeStore()
    app.state.ceiling_service = _FakeCeilingService()
    app.state.licence_service = _FakeLicenceService()
    return app


@pytest.fixture
def wp14_client(wp14_app: FastAPI) -> TestClient:
    return TestClient(wp14_app)


_AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _set_user(fake_verifier: object, role: Role, mfa: SecondFactorState) -> None:
    fake_verifier.users[VALID_TOKEN] = make_user(  # type: ignore[attr-defined]
        uid="u1", role=role, mfa_state=mfa
    )


# ---------------------------------------------------------------------------
# GET /usage requires usage:read
# ---------------------------------------------------------------------------
def test_usage_route_admin_ok(wp14_client: TestClient, fake_verifier: object) -> None:
    _set_user(fake_verifier, Role.ADMIN, SecondFactorState.VERIFIED)
    r = wp14_client.get("/api/v1/usage?tenantId=t1", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["tenantId"] == "t1"
    assert body["ceilingState"] == "OK"


def test_usage_route_radiologist_forbidden(wp14_client: TestClient, fake_verifier: object) -> None:
    _set_user(fake_verifier, Role.RADIOLOGIST, SecondFactorState.VERIFIED)
    r = wp14_client.get("/api/v1/usage?tenantId=t1", headers=_AUTH)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /admin/tenants/{id}/ceiling requires billing:manage + fresh 2FA
# ---------------------------------------------------------------------------
def test_set_ceiling_requires_billing_manage(
    wp14_client: TestClient, fake_verifier: object
) -> None:
    # Radiologist has no billing:manage → 403.
    _set_user(fake_verifier, Role.RADIOLOGIST, SecondFactorState.VERIFIED)
    r = wp14_client.post(
        "/api/v1/admin/tenants/t1/ceiling",
        json={"ceilingUsd": 25.0, "reason": "pilot"},
        headers=_AUTH,
    )
    assert r.status_code == 403


def test_set_ceiling_requires_fresh_mfa(wp14_client: TestClient, fake_verifier: object) -> None:
    # Admin with billing:manage but NO fresh MFA → 403 MFA_REQUIRED.
    _set_user(fake_verifier, Role.ADMIN, SecondFactorState.ENROLLED)
    r = wp14_client.post(
        "/api/v1/admin/tenants/t1/ceiling",
        json={"ceilingUsd": 25.0, "reason": "pilot"},
        headers=_AUTH,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_REQUIRED"


def test_set_ceiling_succeeds_with_capability_and_mfa(
    wp14_app: FastAPI, wp14_client: TestClient, fake_verifier: object
) -> None:
    _set_user(fake_verifier, Role.ADMIN, SecondFactorState.VERIFIED)
    r = wp14_client.post(
        "/api/v1/admin/tenants/t1/ceiling",
        json={"ceilingUsd": 25.0, "reason": "enterprise pilot", "suspended": False},
        headers=_AUTH,
    )
    assert r.status_code == 200
    store: _FakeStore = wp14_app.state.usage_store  # type: ignore[assignment]
    assert store.ceiling_calls == [("t1", 25.0, "enterprise pilot", False)]


# ---------------------------------------------------------------------------
# POST /admin/licence requires billing:manage + fresh 2FA
# ---------------------------------------------------------------------------
def test_issue_licence_requires_fresh_mfa(wp14_client: TestClient, fake_verifier: object) -> None:
    _set_user(fake_verifier, Role.ADMIN, SecondFactorState.ENROLLED)
    r = wp14_client.post(
        "/api/v1/admin/licence",
        json={"licenceToken": "fake.token.sig"},
        headers=_AUTH,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_REQUIRED"


def test_issue_licence_succeeds_with_capability_and_mfa(
    wp14_client: TestClient, fake_verifier: object
) -> None:
    _set_user(fake_verifier, Role.ADMIN, SecondFactorState.VERIFIED)
    r = wp14_client.post(
        "/api/v1/admin/licence",
        json={"licenceToken": "fake.token.sig"},
        headers=_AUTH,
    )
    assert r.status_code == 200
    assert r.json()["siteId"] == "site-1"


# ---------------------------------------------------------------------------
# GET /licence requires authentication only
# ---------------------------------------------------------------------------
def test_licence_route_requires_auth(wp14_client: TestClient) -> None:
    r = wp14_client.get("/api/v1/licence")
    assert r.status_code == 401


def test_licence_route_authed_ok(wp14_client: TestClient, fake_verifier: object) -> None:
    _set_user(fake_verifier, Role.VIEWER, SecondFactorState.VERIFIED)
    r = wp14_client.get("/api/v1/licence", headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["valid"] is True


# ---------------------------------------------------------------------------
# Admin read-usage route requires billing:read
# ---------------------------------------------------------------------------
def test_admin_read_usage_requires_billing_read(
    wp14_client: TestClient, fake_verifier: object
) -> None:
    # Viewer has no billing:read → 403.
    _set_user(fake_verifier, Role.VIEWER, SecondFactorState.VERIFIED)
    r = wp14_client.get("/api/v1/admin/tenants/t1/usage", headers=_AUTH)
    assert r.status_code == 403


def test_admin_read_usage_admin_ok(wp14_client: TestClient, fake_verifier: object) -> None:
    _set_user(fake_verifier, Role.ADMIN, SecondFactorState.VERIFIED)
    r = wp14_client.get("/api/v1/admin/tenants/t1/usage?period=2026-08", headers=_AUTH)
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert body["tenantId"] == "t1"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
