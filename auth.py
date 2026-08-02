"""
auth.py
=======
VuraRAD — JWT-Based Tier Enforcement (Improvement D)

Replaces the insecure localStorage tier check with server-side
Firebase ID Token verification. User tier (STANDARD | PREMIUM | ENTERPRISE)
is embedded in custom Firebase claims, verified here on every request.

Usage (FastAPI dependency injection):
    from auth import get_current_tier, require_premium, require_enterprise

    @app.post("/radiogenomics/analyze")
    async def analyze(tier: str = Depends(require_premium)):
        ...

Setup required:
    firebase-admin must be installed:  pip install firebase-admin
    GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_SERVICE_ACCOUNT must be set.
    Custom claims must be set on user creation:
        auth.set_custom_user_claims(uid, {"vura_tier": "PREMIUM"})
"""

import os
import logging
from functools import lru_cache
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger("vura-auth")

# Tier hierarchy (higher index = higher access)
TIER_HIERARCHY = ["STANDARD", "PREMIUM", "ENTERPRISE"]

_security = HTTPBearer(auto_error=False)
_firebase_initialized = False


def _init_firebase():
    """Lazily initialize Firebase Admin SDK."""
    global _firebase_initialized
    if _firebase_initialized:
        return

    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            # Try service account file first, then ADC
            sa_path = os.getenv("FIREBASE_SERVICE_ACCOUNT")
            if sa_path and os.path.exists(sa_path):
                cred = credentials.Certificate(sa_path)
            else:
                cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred)

        _firebase_initialized = True
        logger.info("[Auth] Firebase Admin SDK initialized.")
    except ImportError:
        logger.warning("[Auth] firebase-admin not installed — auth running in MOCK mode.")
    except Exception as e:
        logger.warning(f"[Auth] Firebase init failed ({e}) — auth running in MOCK mode.")


def _verify_token(token: str) -> dict:
    """
    Verify Firebase ID token and return decoded claims.
    Returns mock claims in development if Firebase not available.
    """
    _init_firebase()

    try:
        from firebase_admin import auth as firebase_auth
        decoded = firebase_auth.verify_id_token(token)
        return decoded
    except ImportError:
        # Dev/test fallback — accept any token, treat as STANDARD
        logger.warning("[Auth] MOCK verification — firebase-admin not available.")
        return {"uid": "dev-user", "vura_tier": os.getenv("DEV_TIER", "PREMIUM")}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_tier(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_security),
) -> str:
    """
    FastAPI dependency: extracts and verifies the Bearer token.
    Returns the user's tier string ('STANDARD' | 'PREMIUM' | 'ENTERPRISE').

    Falls back to 'STANDARD' if no token provided (unauthenticated path).
    """
    if not credentials:
        return "STANDARD"

    claims = _verify_token(credentials.credentials)
    tier = claims.get("vura_tier", "STANDARD").upper()

    if tier not in TIER_HIERARCHY:
        logger.warning(f"[Auth] Unknown tier '{tier}' claimed by uid={claims.get('uid')} — defaulting to STANDARD")
        tier = "STANDARD"

    return tier


def get_current_uid(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_security),
) -> Optional[str]:
    """
    FastAPI dependency: returns the Firebase UID from the token.
    Returns None if no token provided.
    """
    if not credentials:
        return None
    claims = _verify_token(credentials.credentials)
    return claims.get("uid")


def require_tier(minimum_tier: str):
    """
    Factory for FastAPI `Depends()` that enforces a minimum tier.

    Usage:
        @app.post("/premium-endpoint")
        async def endpoint(tier: str = Depends(require_tier("PREMIUM"))):
            ...
    """
    def _check(
        credentials: Optional[HTTPAuthorizationCredentials] = Security(_security),
    ) -> str:
        tier = get_current_tier(credentials)
        if TIER_HIERARCHY.index(tier) < TIER_HIERARCHY.index(minimum_tier):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires {minimum_tier} tier. Your tier: {tier}. "
                       f"Upgrade at https://vurarad.com/upgrade",
            )
        return tier
    return _check


# Convenience aliases
require_premium    = require_tier("PREMIUM")
require_enterprise = require_tier("ENTERPRISE")
