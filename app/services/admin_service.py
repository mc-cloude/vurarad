"""Admin service — Identity Platform user management.

A thin orchestration layer over :mod:`firebase_admin.auth`.  The Firebase SDK
is wrapped behind a :class:`UserDirectory` protocol so the service is unit-
testable without network access — tests inject a fake directory.

Contract:

- ``list_users`` returns :class:`AdminUser` rows with NO PHI (only operator
  identity, role, MFA state, claims version).
- ``set_role`` bumps ``claimsVersion`` **and** calls ``revoke_refresh_tokens``
  so a token minted before the change is rejected (401 TOKEN_REVOKED) on its
  next verification.
- ``disable_user`` flips the account ``disabled`` flag and revokes tokens when
  disabling.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.admin import AdminUser, UserListResponse


# ---------------------------------------------------------------------------
# Raw record — the unprojected user data from Identity Platform
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RawUserRecord:
    """Raw Identity Platform user data before PHI-free projection."""

    uid: str
    email: str | None
    disabled: bool
    custom_claims: dict[str, Any]
    last_sign_in_at: int | None  # epoch millis
    mfa_enrolled: bool


@dataclass(slots=True)
class UserListPage:
    """One page of raw user records."""

    users: list[RawUserRecord]
    next_page_token: str | None


# ---------------------------------------------------------------------------
# Protocol — swap Firebase for a fake in tests
# ---------------------------------------------------------------------------
class UserDirectory(Protocol):
    """Backend-agnostic Identity Platform directory."""

    async def list_users(
        self, *, page_token: str | None, max_results: int
    ) -> UserListPage: ...

    async def get_user(self, uid: str) -> RawUserRecord | None: ...

    async def set_custom_claims(self, uid: str, claims: dict[str, Any]) -> None: ...

    async def revoke_refresh_tokens(self, uid: str) -> None: ...

    async def update_user(self, uid: str, *, disabled: bool) -> None: ...


# ---------------------------------------------------------------------------
# Firebase implementation — wraps the synchronous SDK in asyncio.to_thread
# ---------------------------------------------------------------------------
class FirebaseUserDirectory:
    """Production :class:`UserDirectory` backed by ``firebase_admin.auth``."""

    def __init__(self, app: Any = None) -> None:
        self._app = app

    def _to_raw(self, ur: Any) -> RawUserRecord:
        claims: dict[str, Any] = dict(ur.custom_claims or {})
        ts = getattr(ur.user_metadata, "last_sign_in_timestamp", None)
        last_sign_in = int(ts) if ts else None
        return RawUserRecord(
            uid=ur.uid,
            email=ur.email,
            disabled=bool(ur.disabled),
            custom_claims=claims,
            last_sign_in_at=last_sign_in,
            mfa_enrolled=bool(claims.get("mfaEnrolled", False)),
        )

    def _list_users_sync(
        self, page_token: str | None, max_results: int
    ) -> UserListPage:
        from firebase_admin import auth

        page = auth.list_users(
            page_token=page_token, max_results=max_results, app=self._app
        )
        users = [self._to_raw(ur) for ur in page.users]
        next_token = page.next_page_token if page.has_next_page else None
        return UserListPage(users=users, next_page_token=next_token)

    def _get_user_sync(self, uid: str) -> RawUserRecord | None:
        from firebase_admin import auth
        from firebase_admin.auth import UserNotFoundError

        try:
            ur = auth.get_user(uid, app=self._app)
        except UserNotFoundError:
            return None
        return self._to_raw(ur)

    def _set_claims_sync(self, uid: str, claims: dict[str, Any]) -> None:
        from firebase_admin import auth

        auth.set_custom_user_claims(uid, claims, app=self._app)

    def _revoke_sync(self, uid: str) -> None:
        from firebase_admin import auth

        auth.revoke_refresh_tokens(uid, app=self._app)

    def _update_user_sync(self, uid: str, *, disabled: bool) -> None:
        from firebase_admin import auth

        auth.update_user(uid, disabled=disabled, app=self._app)

    async def list_users(
        self, *, page_token: str | None, max_results: int
    ) -> UserListPage:
        return await asyncio.to_thread(self._list_users_sync, page_token, max_results)

    async def get_user(self, uid: str) -> RawUserRecord | None:
        return await asyncio.to_thread(self._get_user_sync, uid)

    async def set_custom_claims(self, uid: str, claims: dict[str, Any]) -> None:
        await asyncio.to_thread(self._set_claims_sync, uid, claims)

    async def revoke_refresh_tokens(self, uid: str) -> None:
        await asyncio.to_thread(self._revoke_sync, uid)

    async def update_user(self, uid: str, *, disabled: bool) -> None:
        await asyncio.to_thread(self._update_user_sync, uid, disabled=disabled)


# ---------------------------------------------------------------------------
# Projection — raw record → PHI-free AdminUser
# ---------------------------------------------------------------------------
def _to_admin_user(raw: RawUserRecord) -> AdminUser:
    claims = raw.custom_claims
    return AdminUser(
        uid=raw.uid,
        email=raw.email,
        operator_id=str(claims.get("operatorId", "")),
        role=str(claims.get("role", "")),
        disabled=raw.disabled,
        mfa_enrolled=raw.mfa_enrolled,
        last_sign_in_at=raw.last_sign_in_at,
        claims_version=int(claims.get("claimsVersion", 0)),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class AdminService:
    """Orchestrates user listing, role changes, and disable/enable."""

    def __init__(self, directory: UserDirectory) -> None:
        self._directory = directory

    async def list_users(
        self, *, page_token: str | None = None, max_results: int = 100
    ) -> UserListResponse:
        page = await self._directory.list_users(
            page_token=page_token, max_results=max_results
        )
        return UserListResponse(
            users=[_to_admin_user(u) for u in page.users],
            next_page_token=page.next_page_token,
        )

    async def set_role(self, uid: str, role: str) -> AdminUser:
        """Change a user's role.

        Bumps ``claimsVersion`` and immediately revokes refresh tokens so that
        any token minted before the change is rejected on its next verification
        (401 TOKEN_REVOKED).
        """
        raw = await self._directory.get_user(uid)
        if raw is None:
            from app.core.errors import NotFoundError

            raise NotFoundError(f"User {uid} not found")

        claims = dict(raw.custom_claims)
        claims["role"] = role
        claims["claimsVersion"] = int(claims.get("claimsVersion", 0)) + 1
        await self._directory.set_custom_claims(uid, claims)
        await self._directory.revoke_refresh_tokens(uid)

        raw.custom_claims = claims
        return _to_admin_user(raw)

    async def disable_user(self, uid: str, disabled: bool) -> AdminUser:
        """Enable or disable a user account.

        Revokes refresh tokens when disabling so active sessions end promptly.
        """
        raw = await self._directory.get_user(uid)
        if raw is None:
            from app.core.errors import NotFoundError

            raise NotFoundError(f"User {uid} not found")

        await self._directory.update_user(uid, disabled=disabled)
        if disabled:
            await self._directory.revoke_refresh_tokens(uid)
        raw.disabled = disabled
        return _to_admin_user(raw)
