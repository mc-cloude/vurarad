# ruff: noqa: B008
"""Authentication and authorisation — Identity Platform, MFA, RBAC.

Core contracts:
- TokenVerifier: protocol for ID-token verification (real + fake impls)
- get_current_user: FastAPI dependency returning AuthenticatedUser
- require_capability: FastAPI dependency that enforces a single Capability
- require_mfa: FastAPI dependency that enforces second-factor
- SecondFactorVerifier: fresh TOTP assertion verification

This module has NO `except ImportError` and NO `os.getenv` inside any function
reachable from `get_current_user`.  The fail-open paths from `auth.py:79-104`
do not exist here.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from fastapi import Depends, HTTPException, Request, status

from app.core.capabilities import Capability, Role, get_role_capabilities, has_capability


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------
class SecondFactorState(StrEnum):
    UNENROLLED = "UNENROLLED"
    ENROLLED = "ENROLLED"
    VERIFIED = "VERIFIED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    uid: str
    email: str | None
    role: Role
    display_name: str | None = None
    mfa_state: SecondFactorState = SecondFactorState.UNENROLLED
    capabilities: frozenset[Capability] = field(init=False)
    operator_id: str = ""  # ULID, stable per user
    tenant_id: str = "default"  # tenant scope for cross-tenant study access

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", get_role_capabilities(self.role))

    @property
    def is_mfa_verified(self) -> bool:
        return self.mfa_state == SecondFactorState.VERIFIED


# ---------------------------------------------------------------------------
# Protocol — swap real Identity Platform for a fake in tests
# ---------------------------------------------------------------------------
class TokenVerifier(Protocol):
    """Verify an ID token and return the authenticated user."""

    async def verify(self, id_token: str) -> AuthenticatedUser: ...


class SecondFactorVerifier(Protocol):
    """Verify a fresh TOTP challenge."""

    async def verify_totp(self, uid: str, code: str) -> bool: ...


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

_AUTH_HEADER = "Authorization"
_BEARER_RE = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)


def _extract_bearer(request: Request) -> str:
    header = request.headers.get(_AUTH_HEADER, "")
    m = _BEARER_RE.match(header)
    if not m:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "MISSING_TOKEN", "message": "Bearer token required"}},
        )
    return m.group(1)


async def get_current_user(request: Request) -> AuthenticatedUser:
    """Extract, verify, and return the authenticated user.

    Returns 401 when no token is provided — NEVER falls through to a default
    role.  Returns 403 when the token is valid but the role is unrecognised.
    """
    # Extract the bearer token first — a missing token must return 401
    # regardless of whether the verifier is configured.
    token = _extract_bearer(request)
    verifier: TokenVerifier = request.app.state.token_verifier

    try:
        user = await verifier.verify(token)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "TOKEN_INVALID",
                    "message": "Token verification failed",
                }
            },
        ) from exc

    if user.role not in Role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "FORBIDDEN",
                    "message": f"Unknown role: {user.role}",
                }
            },
        )

    return user


def require_capability(
    capability: Capability,
) -> Callable[[Request, AuthenticatedUser], Awaitable[AuthenticatedUser]]:
    """FastAPI dependency factory — enforce a single capability."""

    async def _require(
        request: Request, user: AuthenticatedUser = Depends(get_current_user)
    ) -> AuthenticatedUser:
        if has_capability(user.role, capability):
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": f"Capability '{capability.value}' required",
                }
            },
        )

    return _require


async def require_mfa(
    request: Request, user: AuthenticatedUser = Depends(get_current_user)
) -> AuthenticatedUser:
    """Enforce second-factor.

    - Unenrolled → 403 MFA_ENROLMENT_REQUIRED
    - Enrolled but not verified → 403 MFA_REQUIRED
    - Verified → pass
    Admin sessions are NOT exempt.
    """
    if user.mfa_state == SecondFactorState.UNENROLLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "MFA_ENROLMENT_REQUIRED",
                    "message": "MFA enrolment is required for this operation",
                }
            },
        )
    if user.mfa_state != SecondFactorState.VERIFIED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "MFA_REQUIRED",
                    "message": "Second-factor verification is required",
                }
            },
        )
    return user
