"""WP14 integration — usage API round-trip through the Firestore emulator.

Run::

    FIRESTORE_EMULATOR_HOST=localhost:8200 pytest tests/integration/test_usage_api.py -q

Verifies the Firestore-backed path end to end: ``MeteringService.record()``
→ flush → atomic ``Increment`` → ``CeilingService.compute_state()`` →
``GET /api/v1/usage``.  Requires the Firestore emulator on localhost:8200;
tests skip automatically when it is not reachable.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from itertools import count

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.billing.meters import Meter, MeterUnit
from app.billing.rate_card import RateCard
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.core.config import settings
from app.repositories.usage_repo import FirestoreLicenceStore, UsageRepo
from app.services.ceiling_service import CeilingService
from app.services.licence_service import LicenceService
from app.services.metering_service import MeteringService
from tests.conftest import VALID_TOKEN, make_user

_EMULATOR_HOST = "localhost:8200"


def _emulator_reachable() -> bool:
    """Probe whether the Firestore emulator is listening."""
    try:
        with socket.create_connection(("localhost", 8200), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _emulator_reachable(),
    reason="Firestore emulator not running on localhost:8200",
)

_tenant_prefix = uuid.uuid4().hex[:8]
_tenant_seq = count()
_AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _tenant_id() -> str:
    """Unique tenant per call — avoids cross-test and cross-run contamination."""
    return f"int-{_tenant_prefix}-{next(_tenant_seq)}"


def _set_admin(fake_verifier: object, mfa: SecondFactorState = SecondFactorState.VERIFIED) -> None:
    """Register an admin user on the fake verifier."""
    fake_verifier.users[VALID_TOKEN] = make_user(  # type: ignore[attr-defined]
        uid="u-admin", role=Role.ADMIN, mfa_state=mfa
    )


def _now_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def emulator_env() -> Iterator[None]:
    """Re-enable ``FIRESTORE_EMULATOR_HOST`` for this module.

    ``conftest`` pops it so the production guard stays unit-testable; this
    fixture restores it for the integration tests and removes it on teardown
    so a combined ``pytest tests/`` run does not leak the env var into the
    unit-test suite.
    """
    os.environ["FIRESTORE_EMULATOR_HOST"] = _EMULATOR_HOST
    yield
    os.environ.pop("FIRESTORE_EMULATOR_HOST", None)


@pytest.fixture
def fs_client(emulator_env: None) -> object:
    """Firestore ``AsyncClient`` routed to the emulator by the env var.

    Function-scoped because the gRPC channel binds to the current event loop
    on first async call; a module-scoped client breaks once the first test's
    loop closes.
    """
    from google.cloud.firestore import AsyncClient

    return AsyncClient(project=settings.gcp_project_id, database=settings.firestore_database)


@pytest.fixture
def usage_repo(fs_client: object) -> UsageRepo:
    return UsageRepo(fs_client)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def rate_card() -> RateCard:
    return RateCard.load(settings.gcp_region)


@pytest.fixture
def ceiling_service(usage_repo: UsageRepo, rate_card: RateCard) -> CeilingService:
    return CeilingService(usage_repo, rate_card, settings)


@pytest.fixture
def licence_service(fs_client: object) -> LicenceService:
    return LicenceService(
        FirestoreLicenceStore(fs_client),  # type: ignore[arg-type]
        public_key_pem=settings.licence_public_key_pem,
        grace_days=settings.licence_grace_days,
    )


@pytest.fixture
def int_app(
    app: FastAPI,
    usage_repo: UsageRepo,
    ceiling_service: CeilingService,
    licence_service: LicenceService,
) -> FastAPI:
    """App with real Firestore-backed WP14 services on ``app.state``."""
    app.state.usage_store = usage_repo
    app.state.ceiling_service = ceiling_service
    app.state.licence_service = licence_service
    return app


@pytest.fixture
async def ac(int_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """``httpx.AsyncClient`` backed by the app's ASGI transport."""
    transport = ASGITransport(app=int_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Service-layer round-trip — proves the atomic Increment path in Firestore
# ---------------------------------------------------------------------------
async def test_flush_persists_meters_to_firestore(usage_repo: UsageRepo) -> None:
    """record → flush → get_usage returns the incremented meter values."""
    metering = MeteringService(usage_repo, flush_interval_seconds=999)
    tid = _tenant_id()
    period = _now_period()
    await metering.record(tid, Meter.IMAGES_INGESTED, 500)
    await metering.record(tid, Meter.STUDIES_INGESTED, 3)
    await metering.flush()
    usage = await usage_repo.get_usage(tid, period)
    assert usage is not None
    assert usage.meters["images_ingested"] == 500
    assert usage.meters["studies_ingested"] == 3


async def test_atomic_increment_accumulates(usage_repo: UsageRepo) -> None:
    """Two separate flushes accumulate via Firestore ``Increment``, not overwrite."""
    metering = MeteringService(usage_repo, flush_interval_seconds=999)
    tid = _tenant_id()
    period = _now_period()
    await metering.record(tid, Meter.IMAGES_INGESTED, 300)
    await metering.flush()
    await metering.record(tid, Meter.IMAGES_INGESTED, 300)
    await metering.flush()
    usage = await usage_repo.get_usage(tid, period)
    assert usage is not None
    assert usage.meters["images_ingested"] == 600


# ---------------------------------------------------------------------------
# API routes with real Firestore backing
# ---------------------------------------------------------------------------
async def test_usage_api_returns_metered_data(
    ac: AsyncClient, usage_repo: UsageRepo, fake_verifier: object
) -> None:
    """GET /usage returns meters persisted by the metering flush."""
    _set_admin(fake_verifier)
    metering = MeteringService(usage_repo, flush_interval_seconds=999)
    tid = _tenant_id()
    await metering.record(tid, Meter.IMAGES_INGESTED, 500)
    await metering.record(tid, Meter.STUDIES_INGESTED, 3)
    await metering.flush()
    r = await ac.get(f"/api/v1/usage?tenantId={tid}", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["meters"]["images_ingested"] == 500
    assert body["meters"]["studies_ingested"] == 3
    # studies_ingested costs $0.026 each → 3 × 0.026 = $0.078
    assert body["infraCostUsd"] == pytest.approx(0.078)
    assert body["ceilingState"] == "OK"
    assert body["suspended"] is False


async def test_ceiling_api_persists_and_changes_stage(
    ac: AsyncClient, usage_repo: UsageRepo, fake_verifier: object
) -> None:
    """POST /ceiling persists to Firestore; GET /usage reflects the new stage."""
    _set_admin(fake_verifier)
    metering = MeteringService(usage_repo, flush_interval_seconds=999)
    tid = _tenant_id()
    # 3 studies → $0.078 infra cost.  Ceiling $0.08 → ratio ≈ 0.975 → AI_DISABLED.
    await metering.record(tid, Meter.STUDIES_INGESTED, 3)
    await metering.flush()
    r = await ac.post(
        f"/api/v1/admin/tenants/{tid}/ceiling",
        json={"ceilingUsd": 0.08, "reason": "pilot"},
        headers=_AUTH,
    )
    assert r.status_code == 200
    # POST /ceiling returns CeilingState.model_dump(by_alias=True) — the
    # stage field is a single word so the alias is "stage", not "ceilingState".
    assert r.json()["stage"] == "AI_DISABLED"
    # GET /usage reflects the persisted ceiling.
    r2 = await ac.get(f"/api/v1/usage?tenantId={tid}", headers=_AUTH)
    assert r2.status_code == 200
    body = r2.json()
    assert body["ceilingUsd"] == pytest.approx(0.08)
    assert body["ceilingState"] == "AI_DISABLED"
    assert "ai:use" in body["blockedCapabilities"]


async def test_admin_read_usage_api(
    ac: AsyncClient, usage_repo: UsageRepo, fake_verifier: object
) -> None:
    """GET /admin/tenants/{id}/usage (billing:read) returns the ceiling state."""
    _set_admin(fake_verifier)
    metering = MeteringService(usage_repo, flush_interval_seconds=999)
    tid = _tenant_id()
    await metering.record(tid, Meter.IMAGES_INGESTED, 100)
    await metering.flush()
    r = await ac.get(f"/api/v1/admin/tenants/{tid}/usage", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["tenantId"] == tid
    assert body["stage"] == "OK"


async def test_usage_history_api(
    ac: AsyncClient, usage_repo: UsageRepo, fake_verifier: object
) -> None:
    """GET /usage/history returns periods ordered newest-first."""
    _set_admin(fake_verifier)
    tid = _tenant_id()
    # Write to two past periods directly via the repo.
    await usage_repo.increment_meter(tid, "2026-01", Meter.IMAGES_INGESTED, 100, MeterUnit.COUNT)
    await usage_repo.increment_meter(tid, "2026-02", Meter.IMAGES_INGESTED, 200, MeterUnit.COUNT)
    r = await ac.get(f"/api/v1/usage/history?tenantId={tid}&months=12", headers=_AUTH)
    assert r.status_code == 200
    periods = r.json()
    assert len(periods) == 2
    assert periods[0]["period"] == "2026-02"
    assert periods[1]["period"] == "2026-01"


async def test_overage_calculation(
    ac: AsyncClient, usage_repo: UsageRepo, fake_verifier: object
) -> None:
    """Images beyond the included quota are metered, not estimated (criterion 4)."""
    _set_admin(fake_verifier)
    metering = MeteringService(usage_repo, flush_interval_seconds=999)
    tid = _tenant_id()
    # 150 000 images (included: 120 000) + 600 GB-month storage ($12.00).
    # Ceiling $10.00 → ratio 1.2 → OVERAGE.
    await metering.record(tid, Meter.IMAGES_INGESTED, 150_000)
    await metering.record(tid, Meter.STORAGE_BYTES_MONTH, 600)
    await metering.flush()
    r = await ac.get(f"/api/v1/usage?tenantId={tid}", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["imagesUsed"] == 150_000
    assert body["imagesIncluded"] == 120_000
    assert body["overageImages"] == 30_000
    # 30 000 × $0.25 / 100 = $75.00
    assert body["overageChargeUsd"] == pytest.approx(75.0)
    assert body["ceilingState"] == "OVERAGE"


async def test_licence_route_no_token(ac: AsyncClient, fake_verifier: object) -> None:
    """GET /licence returns valid=false when no token is installed."""
    _set_admin(fake_verifier)
    r = await ac.get("/api/v1/licence", headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["valid"] is False


async def test_ceiling_requires_fresh_mfa(ac: AsyncClient, fake_verifier: object) -> None:
    """POST /ceiling without fresh MFA → 403 MFA_REQUIRED (integration-level)."""
    _set_admin(fake_verifier, mfa=SecondFactorState.ENROLLED)
    tid = _tenant_id()
    r = await ac.post(
        f"/api/v1/admin/tenants/{tid}/ceiling",
        json={"ceilingUsd": 25.0},
        headers=_AUTH,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_REQUIRED"
