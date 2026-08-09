"""MONAI Label authorization proxy — a thin tenant + study gate.

This module is a pure authorization boundary in front of an *external* MONAI
Label server.  It contains **no** MONAI Label, VTK, or upstream imaging-platform
source code (see ``docs/desktop-addon.md`` for the trademark and licensing
constraints).  The desktop add-on connects to vuraRAD over standard DICOMweb
(WP10) for pixel and metadata access; this proxy only fronts the MONAI Label
inference/training REST API.

Responsibilities (acceptance criteria 2–4):

1. **Licence gate** — reject with ``403 FEATURE_NOT_LICENSED`` when the tenant
   licence does not include ``features.slicer_addon``.
2. **Tenant + study authorization** — resolve the referenced study, enforce
   tenant scope (cross-tenant → 404, never 403), and run ``StudyAccessPolicy``
   *before* any byte is forwarded.  An unreadable study yields ``403``.
3. **Size + timeout bounds** — bound the request and response bodies and
   enforce an upstream timeout.  The proxy never streams unbounded bytes and
   never forwards the caller's vuraRAD bearer token to the upstream.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.auth import AuthenticatedUser
from app.core.errors import (
    FeatureNotLicensedError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.models.study import ViewerScope
from app.repositories.study_repo import StudyRepository
from app.services.access_policy import StudyAccessPolicy

logger = logging.getLogger("vurarad.monailabel")

# --------------------------------------------------------------------------- #
# Licence feature gate
# --------------------------------------------------------------------------- #
SLICER_ADDON_FEATURE = "slicer_addon"

# Feature keys exposed via ``GET /licence`` (always present, default ``false``).
KNOWN_FEATURES: tuple[str, ...] = (SLICER_ADDON_FEATURE,)


class LicenceService:
    """Tenant licence feature gate.

    Checks feature flags such as ``slicer_addon`` (the desktop add-on / MONAI
    Label feature).  Production wires the real tenant licence document via
    ``app.state.licence_service``; the default denies every feature — a secure
    fail-closed posture.
    """

    def __init__(self, features: Mapping[str, bool] | None = None) -> None:
        self._features: dict[str, bool] = dict(features) if features else {}

    def get_features(self) -> dict[str, bool]:
        """Return the known feature map (every known key present, default false)."""
        return {name: bool(self._features.get(name, False)) for name in KNOWN_FEATURES}

    def has_feature(self, name: str) -> bool:
        """True only when the named feature is explicitly enabled."""
        return bool(self._features.get(name, False))


# --------------------------------------------------------------------------- #
# Backend abstraction
# --------------------------------------------------------------------------- #
# Default bounds — overridable via the service constructor / ``app.state``.
DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024  # 16 MiB
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # 64 MiB
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class BackendResponse:
    """A fully-materialized, size-bounded response from the MONAI Label backend."""

    status_code: int
    headers: dict[str, str]
    body: bytes


class ResponseTooLargeError(Exception):
    """Internal signal: the backend response stream exceeded the byte bound."""


class MonaiLabelBackend(Protocol):
    """The external MONAI Label server, abstracted for testability."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: str,
        headers: Mapping[str, str],
        content: bytes,
        timeout: float,
        max_response_bytes: int,
    ) -> BackendResponse: ...


class HttpxMonaiLabelBackend:
    """Default :class:`MonaiLabelBackend` backed by ``httpx``.

    Reads the response with a hard byte cap so an oversized upstream cannot
    exhaust memory — the proxy never streams unbounded bytes.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: str,
        headers: Mapping[str, str],
        content: bytes,
        timeout: float,
        max_response_bytes: int,
    ) -> BackendResponse:
        url = f"{self._base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        async with (
            httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
            client.stream(method, url, headers=dict(headers), content=content) as resp,
        ):
            body = bytearray()
            async for chunk in resp.aiter_raw():
                if len(body) + len(chunk) > max_response_bytes:
                    raise ResponseTooLargeError(
                        f"upstream response exceeded {max_response_bytes} bytes"
                    )
                body.extend(chunk)
            return BackendResponse(
                status_code=resp.status_code,
                headers=dict(resp.headers.multi_items()),
                body=bytes(body),
            )


# --------------------------------------------------------------------------- #
# Header hygiene — never leak credentials or hop-by-hop headers upstream
# --------------------------------------------------------------------------- #
_HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Request headers stripped before forwarding.  ``authorization`` carries the
# caller's vuraRAD bearer token and MUST NEVER reach the upstream.
_REQUEST_DROP: frozenset[str] = _HOP_BY_HOP | {
    "authorization",
    "host",
    "content-length",
    "cookie",
    "x-study-id",
    "x-request-id",
}

# Response headers stripped before returning to the caller (Content-Length is
# recomputed by the response layer).
_RESPONSE_DROP: frozenset[str] = _HOP_BY_HOP | {
    "content-length",
    "x-request-id",
}


def _clean_headers(headers: Mapping[str, str], drop: frozenset[str]) -> dict[str, str]:
    """Copy ``headers``, dropping any key whose lowercase name is in ``drop``."""
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in drop:
            continue
        cleaned[key] = value
    return cleaned


# --------------------------------------------------------------------------- #
# Proxy service
# --------------------------------------------------------------------------- #
class MonaiLabelProxyService:
    """Authorize, then forward a single request to the MONAI Label backend."""

    def __init__(
        self,
        *,
        backend: MonaiLabelBackend,
        licence: LicenceService,
        study_repo: StudyRepository,
        viewer_scope: ViewerScope,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._backend = backend
        self._licence = licence
        self._study_repo = study_repo
        self._viewer_scope = viewer_scope
        self._policy = StudyAccessPolicy()
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._timeout_seconds = timeout_seconds

    async def proxy(
        self,
        user: AuthenticatedUser,
        study_id: str,
        path: str,
        method: str,
        query: str,
        request_headers: Mapping[str, str],
        body: bytes,
    ) -> BackendResponse:
        """Authorize the request, then forward it to the MONAI Label backend.

        Raises :class:`ApiError` subclasses that the global handler maps to the
        canonical wire envelope.  Returns a :class:`BackendResponse` for the
        router to emit.
        """
        # 1 — Licence feature gate (criterion 4).
        if not self._licence.has_feature(SLICER_ADDON_FEATURE):
            raise FeatureNotLicensedError(
                "Desktop add-on (MONAI Label) is not licensed for this tenant"
            )

        # 2 — Tenant + study authorization BEFORE the proxy hop (criterion 2).
        study = await self._study_repo.get_study(study_id)
        if study is None or study.tenant_id != user.tenant_id:
            # Cross-tenant resolves to 404, never 403 — no existence leak.
            raise NotFoundError("Study not found")
        self._policy.assert_can_read(user, study, self._viewer_scope)

        # 3 — Request size bound (criterion 3).
        if len(body) > self._max_request_bytes:
            raise PayloadTooLargeError("Request body exceeds the maximum allowed size")

        # 4 — Forward with timeout + response bound.  The caller's bearer token
        #     is stripped by ``_clean_headers`` and never reaches the upstream.
        fwd_headers = _clean_headers(request_headers, _REQUEST_DROP)
        try:
            resp = await self._backend.request(
                method,
                path,
                query=query,
                headers=fwd_headers,
                content=body,
                timeout=self._timeout_seconds,
                max_response_bytes=self._max_response_bytes,
            )
        except ResponseTooLargeError as exc:
            raise PayloadTooLargeError("Response body exceeds the maximum allowed size") from exc
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError("MONAI Label backend timed out") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError("MONAI Label backend is unavailable") from exc

        # 5 — Response size bound (defense-in-depth for backends that do not cap).
        if len(resp.body) > self._max_response_bytes:
            raise PayloadTooLargeError("Response body exceeds the maximum allowed size")

        return BackendResponse(
            status_code=resp.status_code,
            headers=_clean_headers(resp.headers, _RESPONSE_DROP),
            body=resp.body,
        )
