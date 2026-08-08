# ruff: noqa: B008
"""Authentication endpoints.

GET /api/v1/auth/me — return the current user's identity, role, capabilities
and MFA state.  This is the single endpoint that tells the frontend WHO is
logged in and WHAT they can do.
"""

from typing import Any

from fastapi import APIRouter, Depends

from app.core.auth import AuthenticatedUser, get_current_user

router = APIRouter(tags=["auth"])


@router.get("/auth/me")
async def auth_me(user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "operatorId": user.operator_id,
        "uid": user.uid,
        "email": user.email,
        "role": user.role.value,
        "displayName": user.display_name,
        "capabilities": sorted(c.value for c in user.capabilities),
        "mfa": {
            "state": user.mfa_state.value,
            "required": True,
        },
    }
