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
