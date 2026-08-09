# ruff: noqa: B008
"""Desktop add-on router — MONAI Label authorization proxy and licence info.

- ``GET /api/v1/licence`` — expose the tenant licence feature flags, including
  ``features.slicer_addon`` (the desktop add-on / MONAI Label feature).
- ``POST /api/v1/monailabel/{path:path}`` — authorize (``monailabel:use``
  capability + MFA), then proxy to the external MONAI Label backend.

The proxy applies the ``monailabel:use`` capability AND ``StudyAccessPolicy``
on the referenced study BEFORE any byte is forwarded (criterion 2).  The
referenced study is declared by the add-on via the ``X-Study-Id`` header (the
internal study id obtained from the worklist/study-detail API).  Pixel and
metadata access uses standard DICOMweb (WP10) — no bespoke protocol.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.v1.routers.studies_deps import (
    StudyRepoDep,
    ViewerScopeDep,
    require_phi_capability,
)
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.core.errors import PayloadTooLargeError, UpstreamUnavailableError
from app.services.monailabel_proxy import (
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    HttpxMonaiLabelBackend,
    LicenceService,
    MonaiLabelBackend,
    MonaiLabelProxyService,
)

router = APIRouter(tags=["desktop-addon"])


# --------------------------------------------------------------------------- #
# Wire models
# --------------------------------------------------------------------------- #
class LicenceResponse(BaseModel):
    """``GET /licence`` response — the tenant's licence feature flags."""

    features: dict[str, bool]


# --------------------------------------------------------------------------- #
# Dependency providers — tests override via ``app.state``; production wires
# the real backend URL / licence document there.
# --------------------------------------------------------------------------- #
async def get_licence_service(request: Request) -> LicenceService:
    """Return the tenant licence service from ``app.state`` or a deny-default."""
    svc = getattr(request.app.state, "licence_service", None)
    if isinstance(svc, LicenceService):
        return svc
    return LicenceService()  # fail-closed: no feature is licensed by default


LicenceServiceDep = Annotated[LicenceService, Depends(get_licence_service)]


async def get_monailabel_backend(request: Request) -> MonaiLabelBackend:
    """Return the MONAI Label backend from ``app.state`` or build the httpx one."""
    backend = getattr(request.app.state, "monailabel_backend", None)
    if backend is not None:
        return cast("MonaiLabelBackend", backend)
    base_url = getattr(request.app.state, "monailabel_backend_url", "")
    if not base_url:
        raise UpstreamUnavailableError("MONAI Label backend is not configured")
    return HttpxMonaiLabelBackend(str(base_url))


MonaiLabelBackendDep = Annotated[MonaiLabelBackend, Depends(get_monailabel_backend)]


async def get_proxy_service(
    request: Request,
    backend: MonaiLabelBackendDep,
    licence: LicenceServiceDep,
    study_repo: StudyRepoDep,
    viewer_scope: ViewerScopeDep,
) -> MonaiLabelProxyService:
    """Build the proxy service, applying any size/timeout overrides on app.state."""
    max_request_bytes = int(
        getattr(request.app.state, "monailabel_max_request_bytes", DEFAULT_MAX_REQUEST_BYTES)
    )
    max_response_bytes = int(
        getattr(request.app.state, "monailabel_max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)
    )
    timeout_seconds = float(
        getattr(request.app.state, "monailabel_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    )
    return MonaiLabelProxyService(
        backend=backend,
        licence=licence,
        study_repo=study_repo,
        viewer_scope=viewer_scope,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        timeout_seconds=timeout_seconds,
    )


MonaiLabelProxyServiceDep = Annotated[MonaiLabelProxyService, Depends(get_proxy_service)]


# --------------------------------------------------------------------------- #
# Bounded request-body read — never consume unbounded bytes (criterion 3)
# --------------------------------------------------------------------------- #
async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError("Request body exceeds the maximum allowed size")
        chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------- #
# GET /licence — expose feature flags (criterion 4)
# --------------------------------------------------------------------------- #
@router.get(
    "/licence",
    dependencies=[Depends(get_current_user)],
    response_model=LicenceResponse,
)
async def get_licence(licence: LicenceServiceDep) -> LicenceResponse:
    return LicenceResponse(features=licence.get_features())


# --------------------------------------------------------------------------- #
# POST /monailabel/{path:path} — authorize then proxy (criterion 2)
# --------------------------------------------------------------------------- #
@router.post(
    "/monailabel/{path:path}",
    dependencies=[
        Depends(get_current_user),
        Depends(require_mfa),
        Depends(require_phi_capability(Capability.MONAILABEL_USE)),
    ],
)
async def proxy_monailabel(
    path: str,
    request: Request,
    proxy_service: MonaiLabelProxyServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    study_id = request.headers.get("x-study-id", "").strip()
    if not study_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "X-Study-Id header is required",
                }
            },
        )
    body = await _read_bounded_body(
        request,
        int(
            getattr(request.app.state, "monailabel_max_request_bytes", DEFAULT_MAX_REQUEST_BYTES)
        ),
    )
    resp = await proxy_service.proxy(
        user,
        study_id,
        path,
        request.method,
        request.url.query,
        dict(request.headers),
        body,
    )
    return Response(content=resp.body, status_code=resp.status_code, headers=resp.headers)
