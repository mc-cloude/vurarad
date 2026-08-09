# ruff: noqa: B008
"""Integration tests for the MONAI Label authorization proxy (WP20).

Covers: authentication + MFA enforcement, ``monailabel:use`` capability
denials (viewer / admin), the ``features.slicer_addon`` licence gate, tenant +
``StudyAccessPolicy`` authorization BEFORE the proxy hop (unreadable study →
403, cross-tenant → 404), request/response size bounds, upstream timeout /
unavailability, credential stripping, and the ``GET /licence`` feature surface.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.repositories.base import InMemoryDocumentStore
from app.services.monailabel_proxy import (
    BackendResponse,
    LicenceService,
    MonaiLabelBackend,
)
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user

STUDY_SELF = "st_read"  # assigned to the calling radiologist (rad-1)
STUDY_OTHER = "st_other"  # assigned to a different radiologist
STUDY_XTENANT = "st_xtenant"  # belongs to tenant "other"
VALID_TOKEN_ADMIN = "admin-token"
VALID_TOKEN_VIEWER = "viewer-token"
VALID_TOKEN_MFA = "mfa-only-token"


# --------------------------------------------------------------------------- #
# Fake backend — implements MonaiLabelBackend, records calls, can raise
# --------------------------------------------------------------------------- #
class FakeMonaiLabelBackend:
    """In-memory MONAI Label backend that records every forwarded request."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response: BackendResponse = BackendResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"ok": true}',
        )
        self.raise_exc: Exception | None = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: str,
        headers: dict[str, str],
        content: bytes,
        timeout: float,
        max_response_bytes: int,
    ) -> BackendResponse:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "headers": dict(headers),
                "content": content,
                "timeout": timeout,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


# A typed reference so static checkers see the protocol is satisfied.
_BACKEND: MonaiLabelBackend = FakeMonaiLabelBackend()


# --------------------------------------------------------------------------- #
# Study document builder (worklist StudyRecord shape)
# --------------------------------------------------------------------------- #
def _study_doc(
    study_id: str,
    *,
    status: str = "UNREAD",
    assigned_to: dict[str, Any] | None = None,
    tenant_id: str = "default",
) -> dict[str, Any]:
    return {
        "studyId": study_id,
        "patientKey": "pk1",
        "patientRef": "PT-1",
        "patientAgeSex": "50 M",
        "patientSex": "M",
        "patientName": "Doe, John",
        "patientBirthDate": "1976-01-01",
        "mrn": "MRN-1",
        "accession": "ACC-1",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest",
        "studyDate": "2026-08-01T09:00:00Z",
        "referringPhysician": "",
        "clinicalHistory": "",
        "status": status,
        "priority": "ROUTINE",
        "assignedTo": assigned_to,
        "seriesCount": 1,
        "instanceCount": 100,
        "studyBytes": 104857600,
        "hasReport": False,
        "reportId": None,
        "signedAt": None,
        "priorStudies": [],
        "seriesIds": [],
        "tenantId": tenant_id,
        "createdAt": "2026-08-01T09:00:00Z",
        "updatedAt": "2026-08-01T09:00:00Z",
        "version": 1,
    }


def _assigned(uid: str) -> dict[str, Any]:
    return {"uid": uid, "operatorId": "", "displayName": ""}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_backend() -> FakeMonaiLabelBackend:
    return FakeMonaiLabelBackend()


@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    store = InMemoryDocumentStore()
    # Readable by rad-1 (assigned to self).
    asyncio.run(
        store.set("studies", STUDY_SELF, _study_doc(STUDY_SELF, assigned_to=_assigned("rad-1")))
    )
    # Unreadable by rad-1 (assigned to another radiologist).
    asyncio.run(
        store.set(
            "studies", STUDY_OTHER, _study_doc(STUDY_OTHER, assigned_to=_assigned("rad-other"))
        )
    )
    # Cross-tenant study.
    asyncio.run(store.set("studies", STUDY_XTENANT, _study_doc(STUDY_XTENANT, tenant_id="other")))
    return store


@pytest.fixture
def proxy_app(
    fake_backend: FakeMonaiLabelBackend,
    doc_store: InMemoryDocumentStore,
) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(
            uid="rad-1",
            role=Role.RADIOLOGIST,
            mfa_state=SecondFactorState.VERIFIED,
        )
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    app.state.document_store = doc_store
    app.state.monailabel_backend = fake_backend
    app.state.licence_service = LicenceService({"slicer_addon": True})
    app.state.viewer_scopes = {}
    return app


@pytest.fixture
def client(proxy_app: FastAPI) -> TestClient:
    return TestClient(proxy_app)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _proxy_headers(study_id: str, token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Study-Id": study_id}


# --------------------------------------------------------------------------- #
# Authentication, MFA, capability enforcement
# --------------------------------------------------------------------------- #
def test_proxy_requires_authentication(client: TestClient) -> None:
    r = client.post("/api/v1/monailabel/infer/seg", headers={"X-Study-Id": STUDY_SELF})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"


def test_proxy_requires_mfa(proxy_app: FastAPI, client: TestClient) -> None:
    proxy_app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(
            uid="rad-1", role=Role.RADIOLOGIST, mfa_state=SecondFactorState.ENROLLED
        )
    )
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "MFA_REQUIRED"


def test_proxy_denies_viewer_without_capability(
    proxy_app: FastAPI, client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    proxy_app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(uid="viewer-1", role=Role.VIEWER)
    )
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert fake_backend.calls == []  # never reached the proxy hop


def test_proxy_denies_admin_with_phi_access_forbidden(
    proxy_app: FastAPI, client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    proxy_app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(uid="admin-1", role=Role.ADMIN)
    )
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PHI_ACCESS_FORBIDDEN"
    assert fake_backend.calls == []


# --------------------------------------------------------------------------- #
# Licence feature flag (criterion 4)
# --------------------------------------------------------------------------- #
def test_proxy_denies_when_feature_not_licensed(
    proxy_app: FastAPI, client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    proxy_app.state.licence_service = LicenceService({"slicer_addon": False})
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FEATURE_NOT_LICENSED"
    assert fake_backend.calls == []  # licence gate closes before the proxy hop


# --------------------------------------------------------------------------- #
# Tenant + study authorization BEFORE the proxy hop (criterion 2)
# --------------------------------------------------------------------------- #
def test_proxy_denies_unreadable_study(
    proxy_app: FastAPI, client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    """A study assigned to another reader yields 403 and is never proxied."""
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_OTHER), "content-type": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ASSIGNED"
    assert fake_backend.calls == []


def test_proxy_denies_cross_tenant_study(
    proxy_app: FastAPI, client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    """A study in another tenant resolves to 404 (no existence leak), never proxied."""
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_XTENANT), "content-type": "application/json"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert fake_backend.calls == []


def test_proxy_returns_404_for_missing_study(
    client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers("st_missing"), "content-type": "application/json"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert fake_backend.calls == []


def test_proxy_requires_study_id_header(client: TestClient) -> None:
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_auth(), "content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# Authorized proxy hop — forwarding, credential stripping, status pass-through
# --------------------------------------------------------------------------- #
def test_proxy_forwards_authorized_request(
    client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    body = b'{"model":"segmentation"}'
    r = client.post(
        "/api/v1/monailabel/infer/seg?image=foo&params=bar",
        content=body,
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.content == b'{"ok": true}'
    assert r.headers["content-type"] == "application/json"

    assert len(fake_backend.calls) == 1
    call = fake_backend.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "infer/seg"
    assert call["query"] == "image=foo&params=bar"
    assert call["content"] == body

    fwd_keys = {k.lower() for k in call["headers"]}
    # The caller's vuraRAD bearer token must NEVER reach the upstream.
    assert "authorization" not in fwd_keys
    # The vuraRAD-specific study header is not forwarded.
    assert "x-study-id" not in fwd_keys
    # Content-Type is preserved for the upstream.
    assert "content-type" in fwd_keys


def test_proxy_passes_through_upstream_status_code(
    client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    fake_backend.response = BackendResponse(
        status_code=202,
        headers={"content-type": "application/json"},
        body=b'{"accepted": true}',
    )
    r = client.post(
        "/api/v1/monailabel/train/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 202
    assert r.content == b'{"accepted": true}'


# --------------------------------------------------------------------------- #
# Size bounds + timeout (criterion 3) — never stream unbounded bytes
# --------------------------------------------------------------------------- #
def test_proxy_rejects_oversized_request_body(
    proxy_app: FastAPI, client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    proxy_app.state.monailabel_max_request_bytes = 10
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"x" * 100,
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert fake_backend.calls == []  # rejected before the proxy hop


def test_proxy_rejects_oversized_response_body(
    proxy_app: FastAPI, client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    proxy_app.state.monailabel_max_response_bytes = 10
    fake_backend.response = BackendResponse(
        status_code=200,
        headers={"content-type": "application/octet-stream"},
        body=b"x" * 100,
    )
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_proxy_returns_504_on_upstream_timeout(
    client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    fake_backend.raise_exc = httpx.ReadTimeout("simulated upstream timeout")
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_proxy_returns_502_on_upstream_unavailable(
    client: TestClient, fake_backend: FakeMonaiLabelBackend
) -> None:
    fake_backend.raise_exc = httpx.ConnectError("simulated connection refused")
    r = client.post(
        "/api/v1/monailabel/infer/seg",
        content=b"{}",
        headers={**_proxy_headers(STUDY_SELF), "content-type": "application/json"},
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# GET /licence — feature flag surface (criterion 4)
# --------------------------------------------------------------------------- #
def test_get_licence_exposes_slicer_addon_enabled(
    proxy_app: FastAPI, client: TestClient
) -> None:
    proxy_app.state.licence_service = LicenceService({"slicer_addon": True})
    r = client.get("/api/v1/licence", headers=_auth())
    assert r.status_code == 200
    assert r.json()["features"]["slicer_addon"] is True


def test_get_licence_exposes_slicer_addon_disabled(
    proxy_app: FastAPI, client: TestClient
) -> None:
    proxy_app.state.licence_service = LicenceService({"slicer_addon": False})
    r = client.get("/api/v1/licence", headers=_auth())
    assert r.status_code == 200
    assert r.json()["features"]["slicer_addon"] is False


def test_get_licence_defaults_to_deny_when_unconfigured(
    proxy_app: FastAPI, client: TestClient
) -> None:
    proxy_app.state.licence_service = None  # type: ignore[assignment]
    r = client.get("/api/v1/licence", headers=_auth())
    assert r.status_code == 200
    assert r.json()["features"]["slicer_addon"] is False


def test_get_licence_requires_authentication(client: TestClient) -> None:
    r = client.get("/api/v1/licence")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"
