# ruff: noqa: B008
"""Integration tests for the audit API (WP7).

Covers filtered query, the 92-day window cap, mandatory self-audit
(``AUDIT_VIEWED``), chain verification (mirror vs locked-bucket divergence),
NDJSON export with a signed URL and ``AUDIT_EXPORTED`` record, capability
denials (radiologist → 403), and MFA enforcement.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.models.admin import AuditFilter
from app.models.audit import AuditEvent
from app.storage.base import ObjectRef
from tests.conftest import make_user

T0 = 1700000000  # base epoch seconds for seeded events


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _copy(event: AuditEvent) -> AuditEvent:
    return dataclasses.replace(event)


class FakeAuditStore:
    """Combined AuditMirror + AuditReadStore for tests.

    ``write`` appends a sealed event to both the mirror and a snapshot copy in
    the locked-bucket surrogate, so mutating a mirror record afterwards breaks
    chain verification against the bucket.
    """

    def __init__(self) -> None:
        self.mirror: list[AuditEvent] = []
        self.bucket: list[AuditEvent] = []

    # -- AuditMirror --------------------------------------------------------
    async def write(self, event: AuditEvent) -> None:
        self.mirror.append(event)
        self.bucket.append(_copy(event))

    async def read_chain(self, limit: int = 1000) -> list[AuditEvent]:
        return list(reversed(self.mirror))[:limit]

    # -- AuditReadStore -----------------------------------------------------
    @staticmethod
    def _matches(event: AuditEvent, filters: AuditFilter) -> bool:
        if filters.from_ is not None and event.timestamp < filters.from_:
            return False
        if filters.to is not None and event.timestamp > filters.to:
            return False
        if filters.actor is not None and event.actor != filters.actor:
            return False
        if filters.action is not None and event.event_type != filters.action:
            return False
        if filters.patient_key is not None and event.patient_key != filters.patient_key:
            return False
        if filters.study_id is not None:
            return event.detail.get("studyId") == filters.study_id
        return True

    async def query_events(
        self, filters: AuditFilter
    ) -> tuple[list[AuditEvent], str | None]:
        events = sorted(
            (e for e in self.mirror if self._matches(e, filters)),
            key=lambda e: e.seq,
        )
        offset = int(filters.page_token) if filters.page_token else 0
        page = events[offset : offset + filters.limit]
        next_token = (
            str(offset + len(page)) if offset + len(page) < len(events) else None
        )
        return page, next_token

    async def count_events(self, filters: AuditFilter) -> int:
        return len([e for e in self.mirror if self._matches(e, filters)])

    async def read_mirror_chain(self, limit: int = 5000) -> list[AuditEvent]:
        return list(self.mirror)

    async def read_bucket_chain(self, limit: int = 5000) -> list[AuditEvent]:
        return list(self.bucket)

    # -- seeding ------------------------------------------------------------
    def seed_chain(self, specs: list[dict[str, Any]]) -> None:
        prev_hash = AuditEvent.genesis().hash
        seq = 1
        for spec in specs:
            event = AuditEvent(
                seq=seq,
                prev_hash=prev_hash,
                event_type=spec["type"],
                actor=spec["actor"],
                second_factor=True,
                timestamp=spec["ts"],
                detail=spec.get("detail", {}),
                patient_key=spec.get("patient_key", ""),
            )
            event.seal()
            self.mirror.append(event)
            self.bucket.append(_copy(event))
            prev_hash = event.hash
            seq += 1


class FakeExportStore:
    """Minimal object store for audit exports."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, str]] = []

    async def put(
        self, key: str, data: bytes, content_type: str, metadata: Any = None
    ) -> ObjectRef:
        self.puts.append((key, data, content_type))
        return ObjectRef(bucket="vurarad-audit-exports", key=key)

    async def generate_signed_read_url(
        self, key: str, ttl_seconds: int, response_headers: Any = None
    ) -> str:
        return f"https://signed.example/{key}?ttl={ttl_seconds}"


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
def _specs() -> list[dict[str, Any]]:
    return [
        {"type": "STUDY_ACCESSED", "actor": "rad-1", "ts": T0, "patient_key": "pk-a",
         "detail": {"studyId": "st-1"}},
        {"type": "REPORT_SIGNED", "actor": "rad-1", "ts": T0 + 50, "patient_key": "pk-a",
         "detail": {"studyId": "st-1", "resource": "report/r1"}},
        {"type": "STUDY_ACCESSED", "actor": "rad-2", "ts": T0 + 100, "patient_key": "pk-b",
         "detail": {"studyId": "st-2"}},
        {"type": "REPORT_SIGNED", "actor": "rad-2", "ts": T0 + 150, "patient_key": "pk-b",
         "detail": {"studyId": "st-2"}},
    ]


@pytest.fixture
def audit_store() -> FakeAuditStore:
    store = FakeAuditStore()
    store.seed_chain(_specs())
    return store


@pytest.fixture
def export_store() -> FakeExportStore:
    return FakeExportStore()


@pytest.fixture
def audit_app(
    audit_store: FakeAuditStore,
    export_store: FakeExportStore,
) -> FastAPI:
    from app.main import create_app

    verifier = StubTokenVerifier(
        users={
            "admin-token": make_user(
                uid="admin-uid", role=Role.ADMIN, mfa_state=SecondFactorState.VERIFIED
            ),
            "rad-token": make_user(
                uid="rad-uid", role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED
            ),
        }
    )
    app = create_app()
    app.state.token_verifier = verifier
    app.state.audit_mirror = audit_store
    app.state.audit_read_store = audit_store
    app.state.export_object_store = export_store
    return app


@pytest.fixture
def client(audit_app: FastAPI) -> TestClient:
    return TestClient(audit_app)


def _auth(token: str = "admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. GET /audit — filters, window cap, self-audit, chain verification
# ---------------------------------------------------------------------------
class TestAuditQuery:
    def test_requires_from_and_to(self, client: TestClient) -> None:
        r = client.get("/api/v1/audit", headers=_auth())
        assert r.status_code == 422

    def test_window_too_wide_returns_422(self, client: TestClient) -> None:
        r = client.get(
            f"/api/v1/audit?from={T0}&to={T0 + 93 * 86400}", headers=_auth()
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "AUDIT_WINDOW_TOO_WIDE"

    def test_valid_window_returns_entries(self, client: TestClient) -> None:
        r = client.get(f"/api/v1/audit?from={T0}&to={T0 + 200}", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["totalCount"] == 4
        assert len(body["entries"]) == 4
        assert body["chainVerified"] is True
        actions = {e["action"] for e in body["entries"]}
        assert actions == {"STUDY_ACCESSED", "REPORT_SIGNED"}

    def test_actor_filter_narrows_results(self, client: TestClient) -> None:
        r = client.get(
            f"/api/v1/audit?from={T0}&to={T0 + 200}&actor=rad-2", headers=_auth()
        )
        assert r.status_code == 200
        body = r.json()
        assert body["totalCount"] == 2
        assert all(e["actor"] == "rad-2" for e in body["entries"])

    def test_action_filter_narrows_results(self, client: TestClient) -> None:
        r = client.get(
            f"/api/v1/audit?from={T0}&to={T0 + 200}&action=REPORT_SIGNED",
            headers=_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["totalCount"] == 2
        assert all(e["action"] == "REPORT_SIGNED" for e in body["entries"])

    def test_patient_key_filter_narrows_results(self, client: TestClient) -> None:
        r = client.get(
            f"/api/v1/audit?from={T0}&to={T0 + 200}&patientKey=pk-b", headers=_auth()
        )
        assert r.status_code == 200
        body = r.json()
        assert body["totalCount"] == 2
        assert all(e["patientKey"] == "pk-b" for e in body["entries"])

    def test_pagination_returns_next_token(self, client: TestClient) -> None:
        r = client.get(
            f"/api/v1/audit?from={T0}&to={T0 + 200}&limit=2", headers=_auth()
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["entries"]) == 2
        assert body["nextPageToken"] is not None

    def test_every_query_writes_audit_viewed(
        self, client: TestClient, audit_store: FakeAuditStore
    ) -> None:
        before = sum(1 for e in audit_store.mirror if e.event_type == "AUDIT_VIEWED")
        r = client.get(
            f"/api/v1/audit?from={T0}&to={T0 + 200}&actor=rad-1", headers=_auth()
        )
        assert r.status_code == 200
        after = sum(1 for e in audit_store.mirror if e.event_type == "AUDIT_VIEWED")
        assert after == before + 1
        viewed = next(e for e in audit_store.mirror if e.event_type == "AUDIT_VIEWED")
        assert viewed.detail.get("actorFilter") == "rad-1"
        assert viewed.detail.get("from") == T0
        assert viewed.detail.get("to") == T0 + 200

    def test_mutated_mirror_yields_chain_not_verified(
        self, client: TestClient, audit_store: FakeAuditStore
    ) -> None:
        # Tamper with a mirror record's hash — the bucket copy is unchanged.
        object.__setattr__(audit_store.mirror[1], "hash", "deadbeef")
        r = client.get(f"/api/v1/audit?from={T0}&to={T0 + 200}", headers=_auth())
        assert r.status_code == 200
        assert r.json()["chainVerified"] is False


# ---------------------------------------------------------------------------
# 2. POST /audit/exports — NDJSON, signed URL, AUDIT_EXPORTED
# ---------------------------------------------------------------------------
class TestAuditExport:
    def test_export_returns_signed_url_and_count(
        self, client: TestClient, export_store: FakeExportStore
    ) -> None:
        r = client.post(
            "/api/v1/audit/exports",
            json={"from": T0, "to": T0 + 200, "reason": "compliance audit", "recipient": "cso@org"},
            headers=_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["recordCount"] == 4
        assert body["signedUrl"].startswith("https://signed.example/")
        assert body["sha256"]
        assert body["exportId"]
        # The NDJSON was uploaded to the export bucket.
        assert len(export_store.puts) == 1
        key, data, content_type = export_store.puts[0]
        assert key.endswith(".ndjson")
        assert content_type == "application/x-ndjson"
        lines = [ln for ln in data.decode().splitlines() if ln]
        assert len(lines) == 4
        # SHA-256 matches the uploaded bytes.
        import hashlib

        assert hashlib.sha256(data).hexdigest() == body["sha256"]

    def test_export_writes_audit_exported_record(
        self, client: TestClient, audit_store: FakeAuditStore
    ) -> None:
        r = client.post(
            "/api/v1/audit/exports",
            json={"from": T0, "to": T0 + 200, "reason": "legal hold", "recipient": "legal@org"},
            headers=_auth(),
        )
        assert r.status_code == 200
        exported = next(e for e in audit_store.mirror if e.event_type == "AUDIT_EXPORTED")
        assert exported.detail.get("reason") == "legal hold"
        assert exported.detail.get("recipient") == "legal@org"
        assert exported.detail.get("recordCount") == 4
        assert exported.detail.get("sha256") == r.json()["sha256"]

    def test_export_window_too_wide_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/audit/exports",
            json={"from": T0, "to": T0 + 93 * 86400, "reason": "x", "recipient": "y"},
            headers=_auth(),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "AUDIT_WINDOW_TOO_WIDE"

    def test_export_ndjson_is_valid_json(
        self, client: TestClient, export_store: FakeExportStore
    ) -> None:
        client.post(
            "/api/v1/audit/exports",
            json={"from": T0, "to": T0 + 200, "reason": "x", "recipient": "y"},
            headers=_auth(),
        )
        _key, data, _ct = export_store.puts[0]
        for line in data.decode().splitlines():
            obj = json.loads(line)
            assert "seq" in obj
            assert "action" in obj
            assert "hash" in obj


# ---------------------------------------------------------------------------
# 3. Capability denials + MFA
# ---------------------------------------------------------------------------
class TestAuditDenials:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("GET", "/api/v1/audit"), ("POST", "/api/v1/audit/exports")],
    )
    def test_radiologist_forbidden(self, client: TestClient, method: str, path: str) -> None:
        if method == "GET":
            r = client.get(f"{path}?from={T0}&to={T0 + 200}", headers=_auth("rad-token"))
        else:
            r = client.post(
                path,
                json={"from": T0, "to": T0 + 200, "reason": "x", "recipient": "y"},
                headers=_auth("rad-token"),
            )
        assert r.status_code == 403

    def test_first_factor_only_admin_gets_mfa_required(self, audit_app: FastAPI) -> None:
        verifier = audit_app.state.token_verifier
        assert isinstance(verifier, StubTokenVerifier)
        verifier.users["enrolled-only"] = make_user(
            uid="admin-2", role=Role.ADMIN, mfa_state=SecondFactorState.ENROLLED
        )
        client = TestClient(audit_app)
        r = client.get(f"/api/v1/audit?from={T0}&to={T0 + 200}", headers=_auth("enrolled-only"))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "MFA_REQUIRED"
