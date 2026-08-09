# ruff: noqa: B008
"""Route-level security invariants for /dicomweb/* and /api/v1/*.

Asserts that PUBLIC_ROUTES has exactly 2 entries, every non-public route
requires authentication, and every /dicomweb/* path carries auth, MFA,
exactly one capability, and a StudyAccessPolicy on study-scoped paths.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.security import PUBLIC_ROUTES
from app.main import create_app


# ---------------------------------------------------------------------------
# Helpers — flatten _IncludedRouter objects and walk dependency trees
# ---------------------------------------------------------------------------
def _collect_api_routes(
    app: FastAPI,
) -> list[tuple[str, str, APIRoute]]:
    """Return (method, full_path, route) for every API route in the app."""
    result: list[tuple[str, str, APIRoute]] = []
    for r in app.routes:
        if type(r).__name__ == "_IncludedRouter":
            prefix = r.include_context.prefix or ""
            for route in r.original_router.routes:
                if isinstance(route, APIRoute):
                    full_path = prefix + route.path
                    for method in sorted(route.methods - {"HEAD"}):
                        result.append((method, full_path, route))
        elif isinstance(r, APIRoute):
            for method in sorted(r.methods - {"HEAD"}):
                result.append((method, r.path, r))
    return result


def _walk_deps(route: APIRoute) -> list[Any]:
    """Return a flat list of all dependency callables for a route."""
    calls: list[Any] = []
    for d in route.dependencies:
        if d.dependency:
            calls.append(d.dependency)
    if route.dependant:
        for d in route.dependant.dependencies:
            calls.append(d.call)
    return calls


def _dep_names(route: APIRoute) -> set[str]:
    """Return the set of ``__name__`` for all dependency callables."""
    return {getattr(c, "__name__", str(c)) for c in _walk_deps(route)}


def _require_capability_calls(route: APIRoute) -> list[Any]:
    """Return the unique ``_require`` closure(s) from ``require_capability``."""
    seen: set[int] = set()
    result: list[Any] = []
    for c in _walk_deps(route):
        if getattr(c, "__name__", "") == "_require" and id(c) not in seen:
            seen.add(id(c))
            result.append(c)
    return result


def _extract_capability(call: Any) -> Any:
    """Extract the Capability value from a ``_require`` closure."""
    if hasattr(call, "__closure__") and call.__closure__:
        for cell in call.__closure__:
            val = cell.cell_contents
            if hasattr(val, "value"):
                return val
    return None


def _dicomweb_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """Filter to /dicomweb/ routes only."""
    return [(m, p, r) for m, p, r in _collect_api_routes(app) if p.startswith("/dicomweb/")]


# ---------------------------------------------------------------------------
# Expected capability inventory for /dicomweb/* routes
# ---------------------------------------------------------------------------
_DICOMWEB_CAPABILITIES: dict[tuple[str, str], str] = {
    ("GET", "/dicomweb/studies"): "study:search",
    ("POST", "/dicomweb/studies"): "study:import",
}


# Routes that are NOT study-scoped (no StudyAccessPolicy needed)
_NON_SCOPED: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/dicomweb/studies"),
        ("POST", "/dicomweb/studies"),
    }
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_public_allowlist_is_exactly_two_entries() -> None:
    """PUBLIC_ROUTES must contain exactly healthz and readyz."""
    assert len(PUBLIC_ROUTES) == 2
    for _method, path in PUBLIC_ROUTES:
        assert path in ("/healthz", "/readyz")


def test_every_non_public_route_requires_authentication() -> None:
    """Every route NOT in PUBLIC_ROUTES must depend on get_current_user."""
    app = create_app()
    for method, path, route in _collect_api_routes(app):
        if (method, path) in PUBLIC_ROUTES:
            continue
        names = _dep_names(route)
        assert "get_current_user" in names, f"Route {method} {path} does not require authentication"


def test_dicomweb_routes_require_auth_and_mfa() -> None:
    """Every /dicomweb/* route must carry get_current_user and require_mfa."""
    app = create_app()
    routes = _dicomweb_routes(app)
    assert len(routes) == 10, f"Expected 10 dicomweb routes, got {len(routes)}"
    for method, path, route in routes:
        names = _dep_names(route)
        assert "get_current_user" in names, f"Route {method} {path} missing get_current_user"
        assert "require_mfa" in names, f"Route {method} {path} missing require_mfa"


def test_dicomweb_routes_declare_exactly_one_capability() -> None:
    """Every /dicomweb/* route must declare exactly one capability."""
    app = create_app()
    for method, path, route in _dicomweb_routes(app):
        caps = _require_capability_calls(route)
        assert len(caps) == 1, (
            f"Route {method} {path} declares {len(caps)} capabilities, expected 1"
        )
        cap = _extract_capability(caps[0])
        assert cap is not None, f"Route {method} {path} capability closure has no Capability value"


def test_dicomweb_route_capabilities_match_inventory() -> None:
    """The declared capability must match the expected inventory."""
    from app.core.capabilities import Capability

    app = create_app()
    for method, path, route in _dicomweb_routes(app):
        caps = _require_capability_calls(route)
        cap = _extract_capability(caps[0])
        expected = _DICOMWEB_CAPABILITIES.get((method, path), "study:read")
        assert cap == Capability(expected), (
            f"Route {method} {path} has capability {cap}, expected {expected}"
        )


def test_dicomweb_study_scoped_routes_have_study_access_policy() -> None:
    """Study-scoped /dicomweb/* routes must carry resolve_study (StudyAccessPolicy)."""
    app = create_app()
    for method, path, route in _dicomweb_routes(app):
        if (method, path) in _NON_SCOPED:
            continue
        names = _dep_names(route)
        assert "resolve_study" in names, (
            f"Route {method} {path} is study-scoped but missing resolve_study"
        )


def test_no_dicomweb_route_in_public_routes() -> None:
    """No /dicomweb/* path may appear in PUBLIC_ROUTES."""
    for _method, path in PUBLIC_ROUTES:
        assert not path.startswith("/dicomweb/"), (
            f"dicomweb route {path} must not be in PUBLIC_ROUTES"
        )


_FORBIDDEN_PARAM_NAMES = frozenset({"study_instance_uid", "patient_name", "mrn", "patient_id"})


def test_no_route_path_contains_direct_identifier_param() -> None:
    """No route path may use a direct-identifier parameter name."""
    app = create_app()
    for _method, path, _route in _collect_api_routes(app):
        for forbidden in _FORBIDDEN_PARAM_NAMES:
            assert f"{{{forbidden}}}" not in path, (
                f"Route {path} contains forbidden param name: {forbidden}"
            )


# ---------------------------------------------------------------------------
# /api/v1/studies/* route security (WP4 — §3.3–3.6 acceptance criteria 11–15)
# ---------------------------------------------------------------------------
def _studies_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """Filter to /api/v1/studies routes only (excluding WP12 sub-resources)."""
    return [
        (m, p, r)
        for m, p, r in _collect_api_routes(app)
        if p.startswith("/api/v1/studies") and "/findings" not in p and "/preprocessing" not in p
    ]


def test_studies_route_count_is_six() -> None:
    """The studies package exposes exactly six routes (§3.3–3.6)."""
    app = create_app()
    routes = _studies_routes(app)
    assert len(routes) == 6, f"Expected 6 studies routes, got {len(routes)}"


def test_studies_routes_require_auth_and_mfa() -> None:
    """Every /api/v1/studies/* route must carry get_current_user and require_mfa.

    A first-factor-only token gets 403 MFA_REQUIRED on every route in this
    package because ``require_mfa`` rejects before any capability check
    (acceptance criterion 15).
    """
    app = create_app()
    for method, path, route in _studies_routes(app):
        names = _dep_names(route)
        assert "get_current_user" in names, f"Route {method} {path} missing get_current_user"
        assert "require_mfa" in names, f"Route {method} {path} missing require_mfa"


def test_studies_routes_have_phi_capability_check() -> None:
    """Every /api/v1/studies/* route must carry a PHI capability check.

    Each route has either ``require_phi_capability`` (the ``_require`` closure)
    or ``require_patient_identity_access`` as a route-level dependency, so an
    admin gets 403 PHI_ACCESS_FORBIDDEN on every route (criterion 11) and a
    viewer is denied patient-identity access (criterion 12).
    """
    app = create_app()
    for method, path, route in _studies_routes(app):
        names = _dep_names(route)
        assert "_require" in names or "require_patient_identity_access" in names, (
            f"Route {method} {path} missing PHI capability check"
        )


# ---------------------------------------------------------------------------
# /api/v1/admin, /audit, /analytics route security (WP7)
# ---------------------------------------------------------------------------
def _admin_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    return [(m, p, r) for m, p, r in _collect_api_routes(app) if p.startswith("/api/v1/admin")]


def _audit_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    return [(m, p, r) for m, p, r in _collect_api_routes(app) if p.startswith("/api/v1/audit")]


def _analytics_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    return [(m, p, r) for m, p, r in _collect_api_routes(app) if p.startswith("/api/v1/analytics")]


def test_admin_route_count_is_four() -> None:
    app = create_app()
    assert len(_admin_routes(app)) == 4


def test_audit_route_count_is_two() -> None:
    app = create_app()
    assert len(_audit_routes(app)) == 2


def test_analytics_route_count_is_one() -> None:
    app = create_app()
    assert len(_analytics_routes(app)) == 1


def test_admin_audit_analytics_routes_require_auth_and_mfa() -> None:
    """Every admin/audit/analytics route carries get_current_user + require_mfa."""
    app = create_app()
    for method, path, route in [*_admin_routes(app), *_audit_routes(app), *_analytics_routes(app)]:
        names = _dep_names(route)
        assert "get_current_user" in names, f"Route {method} {path} missing get_current_user"
        assert "require_mfa" in names, f"Route {method} {path} missing require_mfa"


def test_admin_audit_analytics_routes_declare_one_capability() -> None:
    """Every admin/audit/analytics route declares exactly one capability."""
    app = create_app()
    for method, path, route in [
        *_admin_routes(app),
        *_audit_routes(app),
        *_analytics_routes(app),
    ]:
        caps = _require_capability_calls(route)
        assert len(caps) == 1, (
            f"Route {method} {path} declares {len(caps)} capabilities, expected 1"
        )
        cap = _extract_capability(caps[0])
        assert cap is not None, f"Route {method} {path} capability closure empty"


# Expected capability inventory for the WP7 routes.
_WP7_CAPABILITIES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/admin/users"): "user:manage",
    ("POST", "/api/v1/admin/users/{uid}/role"): "user:manage",
    ("POST", "/api/v1/admin/users/{uid}/disable"): "user:manage",
    ("DELETE", "/api/v1/admin/patients/{patient_key}"): "compliance:purge",
    ("GET", "/api/v1/audit"): "audit:read",
    ("POST", "/api/v1/audit/exports"): "audit:export",
    ("GET", "/api/v1/analytics/dashboard"): "analytics:read",
}


def test_wp7_route_capabilities_match_inventory() -> None:
    """The declared capability matches the expected inventory for each WP7 route."""
    from app.core.capabilities import Capability

    app = create_app()
    for method, path, route in [
        *_admin_routes(app),
        *_audit_routes(app),
        *_analytics_routes(app),
    ]:
        cap = _extract_capability(_require_capability_calls(route)[0])
        expected = _WP7_CAPABILITIES[(method, path)]
        assert cap == Capability(expected), (
            f"Route {method} {path} has capability {cap}, expected {expected}"
        )


def test_wp7_route_capabilities_denied_to_radiologist() -> None:
    """Every capability required by a WP7 route is absent from the radiologist
    role — so a radiologist gets 403 on every admin/audit route and on analytics
    (acceptance criterion 13)."""
    from app.core.capabilities import Role, has_capability

    app = create_app()
    for method, path, route in [
        *_admin_routes(app),
        *_audit_routes(app),
        *_analytics_routes(app),
    ]:
        cap = _extract_capability(_require_capability_calls(route)[0])
        assert cap is not None
        assert has_capability(Role.RADIOLOGIST, cap) is False, (
            f"Route {method} {path} requires {cap} which a radiologist must NOT hold"
        )
