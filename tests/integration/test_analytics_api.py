# ruff: noqa: B008
"""Integration tests for the analytics API (WP7).

The dashboard is PHI-free, so admin (who holds ``analytics:read`` but zero
PHI-read capabilities) is permitted.  These tests prove the dashboard counters
*measurably change* after simulated ingest and sign operations, that the
compliance block is present with no PHI fields, and that radiologist/viewer
(who lack ``analytics:read``) get ``403`` plus MFA enforcement.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.services.analytics_service import AnalyticsService
from tests.conftest import make_user


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class RecordingCounterStore:
    """In-memory ``AnalyticsCounterStore`` that records every increment."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.increment_calls: list[tuple[str, int]] = []

    async def increment(self, counter_name: str, amount: int = 1) -> int:
        self.increment_calls.append((counter_name, amount))
        self.counters[counter_name] = self.counters.get(counter_name, 0) + amount
        return self.counters[counter_name]

    async def read(self, counter_name: str) -> int:
        return self.counters.get(counter_name, 0)

    async def read_prefix(self, prefix: str) -> dict[str, int]:
        return {
            name: value
            for name, value in self.counters.items()
            if name.startswith(prefix)
        }


class StubTokenVerifier:
    """Token verifier mapping a few tokens to fixed users."""

    def __init__(self, users: dict[str, Any]) -> None:
        self.users = users

    async def verify(self, id_token: str) -> Any:
        user = self.users.get(id_token)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "TOKEN_INVALID", "message": "Unknown token"}},
            )
        return user


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def counter_store() -> RecordingCounterStore:
    return RecordingCounterStore()


@pytest.fixture
def analytics_app(counter_store: RecordingCounterStore) -> FastAPI:
    from app.main import create_app

    verifier = StubTokenVerifier(
        users={
            "admin-token": make_user(
                uid="admin-uid", role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED
            ),
            "rad-token": make_user(
                uid="rad-uid", role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED
            ),
            "viewer-token": make_user(
                uid="viewer-uid", role=Role.VIEWER, mfa_state=SecondFactorState.VERIFIED
            ),
        }
    )
    app = create_app()
    app.state.token_verifier = verifier
    app.state.analytics_counter_store = counter_store
    return app


@pytest.fixture
def client(analytics_app: FastAPI) -> TestClient:
    return TestClient(analytics_app)


def _auth(token: str = "admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _dashboard(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/v1/analytics/dashboard", headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Dashboard counters measurably change after ingest + sign
# ---------------------------------------------------------------------------
class TestDashboardCounters:
    def test_empty_dashboard_starts_at_zero(self, client: TestClient) -> None:
        body = _dashboard(client)
        assert body["totalStudies"] == 0
        assert body["totalReports"] == 0
        assert body["signedReports"] == 0
        assert body["studiesByModality"] == {}
        assert body["aiUsage"] == 0

    async def test_counters_change_after_ingest_and_sign(
        self,
        client: TestClient,
        counter_store: RecordingCounterStore,
        analytics_app: FastAPI,
    ) -> None:
        before = _dashboard(client)
        assert before["totalStudies"] == 0
        assert before["signedReports"] == 0

        # Simulate two ingests (one CT, one MR) and one report sign via the
        # same counter store the dashboard reads from.
        service = AnalyticsService(counter_store)
        await service.study_ingested("CT")
        await service.study_ingested("MR")
        await service.report_created()
        await service.report_signed()

        after = _dashboard(client)
        assert after["totalStudies"] == 2
        assert after["totalReports"] == 1
        assert after["signedReports"] == 1
        # Per-modality breakdown is keyed by bare modality name.
        assert after["studiesByModality"] == {"CT": 1, "MR": 1}

        # The increments actually flowed through the wired store.
        names = [name for name, _ in counter_store.increment_calls]
        assert "total_studies" in names
        assert "studies_by_modality_CT" in names
        assert "reports_signed" in names

    async def test_compliance_erasures_counter_changes(
        self,
        client: TestClient,
        counter_store: RecordingCounterStore,
    ) -> None:
        before = _dashboard(client)
        assert before["compliance"]["erasuresPerformed"] == 0

        service = AnalyticsService(counter_store)
        await service.erasure_performed()

        after = _dashboard(client)
        assert after["compliance"]["erasuresPerformed"] == 1

    async def test_audit_chain_length_reflected(
        self,
        client: TestClient,
        counter_store: RecordingCounterStore,
    ) -> None:
        service = AnalyticsService(counter_store)
        await service.increment_audit_chain_length()
        await service.increment_audit_chain_length()

        body = _dashboard(client)
        assert body["compliance"]["auditChainLength"] == 2


# ---------------------------------------------------------------------------
# 2. Compliance block present + no PHI
# ---------------------------------------------------------------------------
class TestComplianceAndPhi:
    def test_compliance_block_present(self, client: TestClient) -> None:
        body = _dashboard(client)
        compliance = body["compliance"]
        for key in (
            "auditChainLength",
            "auditChainVerified",
            "erasuresPerformed",
            "usersMfaEnrolled",
        ):
            assert key in compliance, f"missing compliance key {key}"
        assert compliance["auditChainVerified"] is True

    def test_dashboard_has_zero_phi_fields(self, client: TestClient) -> None:
        body = _dashboard(client)
        # The whole response must be free of patient identifiers.
        serialized = repr(body)
        for phi in ("patientName", "patientBirthDate", "mrn", "patientRef", "patientKey"):
            assert phi.lower() not in serialized.lower(), f"PHI term {phi!r} present"


# ---------------------------------------------------------------------------
# 3. Capability denials — radiologist/viewer → 403
# ---------------------------------------------------------------------------
class TestAnalyticsDenials:
    @pytest.mark.parametrize("token", ["rad-token", "viewer-token"])
    def test_non_admin_forbidden(self, client: TestClient, token: str) -> None:
        r = client.get("/api/v1/analytics/dashboard", headers=_auth(token))
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 4. MFA enforcement
# ---------------------------------------------------------------------------
class TestAnalyticsMfa:
    def test_first_factor_only_admin_gets_mfa_required(
        self, analytics_app: FastAPI
    ) -> None:
        verifier = analytics_app.state.token_verifier
        assert isinstance(verifier, StubTokenVerifier)
        verifier.users["enrolled-only"] = make_user(
            uid="admin-2", role=Role.ADMIN, mfa_state=SecondFactorState.ENROLLED
        )
        client = TestClient(analytics_app)
        r = client.get("/api/v1/analytics/dashboard", headers=_auth("enrolled-only"))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"
