"""On-prem licence verification — detached JWS, offline, public key (§3.19.4).

The licence is a JWS (RFC 7515 compact serialisation) signed by an offline key
and verified against ``licence_public_key_pem``.  Verification is fully
offline — there is no outbound call, and no shared secret.  The payload carries
``siteId``, ``seats``, ``tier``, ``features``, ``notBefore`` and ``notAfter``.

Expiry starts a ``licence_grace_days`` grace period during which the system
logs ``LICENCE_EXPIRED`` daily and shows a banner but blocks nothing.  After
grace, ingest and AI disable while the viewer and report signing keep working —
bricking a hospital's reading workflow over a lapsed licence is not acceptable.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import Field

from app.core.capabilities import Capability
from app.core.errors import LicenceInvalidError
from app.models.common import CamelModel

logger = logging.getLogger("vurarad.licence")

_SECONDS_PER_DAY = 86400

# Capabilities disabled after the licence grace period expires.  Reading and
# report signing are NEVER disabled by a lapsed licence.
_LICENCE_BLOCKED: frozenset[Capability] = frozenset(
    {
        Capability.STUDY_IMPORT,
        Capability.STUDY_WRITE,
        Capability.AI_USE,
        Capability.RESEARCH_DRAFT,
    }
)


class LicenceClaims(CamelModel):
    """The licence payload, recovered from the verified JWS."""

    site_id: str
    seats: int
    tier: str
    features: list[str] = Field(default_factory=list)
    not_before: int
    not_after: int


class LicenceState(CamelModel):
    """The evaluated licence status returned by ``GET /api/v1/licence``."""

    valid: bool
    site_id: str | None = None
    tier: str | None = None
    seats: int | None = None
    features: list[str] = Field(default_factory=list)
    not_after: int | None = None
    expired: bool = False
    in_grace: bool = False
    grace_days_left: int = 0
    blocking_capabilities: list[str] = Field(default_factory=list)


class LicenceStore(Protocol):
    """Persistence boundary for the installed licence token."""

    async def get(self) -> str | None: ...

    async def set(self, token: str) -> None: ...


# ---------------------------------------------------------------------------
# JWS helpers (RFC 7515 compact serialisation, RS256)
# ---------------------------------------------------------------------------
def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class LicenceService:
    """Verifies and evaluates licences offline against the configured public key."""

    def __init__(self, store: LicenceStore, *, public_key_pem: str | None, grace_days: int) -> None:
        self._store = store
        self._grace_days = grace_days
        self._public_key: rsa.RSAPublicKey | None = self._load_public_key(public_key_pem)

    @staticmethod
    def _load_public_key(pem: str | None) -> rsa.RSAPublicKey | None:
        if not pem:
            return None
        loaded = serialization.load_pem_public_key(pem.encode())
        if not isinstance(loaded, rsa.RSAPublicKey):
            raise TypeError("licence_public_key_pem must be an RSA public key")
        return loaded

    def verify_token(self, token: str) -> LicenceClaims:
        """Verify the detached JWS signature and return the claims.

        Raises :class:`LicenceInvalidError` if no key is configured, the token
        is malformed, the algorithm is unsupported, or the signature is invalid.
        Expiry is NOT checked here — it is a property of the claims, evaluated
        separately by :meth:`evaluate`.
        """
        if self._public_key is None:
            raise LicenceInvalidError("licence_public_key_pem is not configured")
        parts = token.split(".")
        if len(parts) != 3:
            raise LicenceInvalidError("licence token is not a valid JWS")
        header_b64, payload_b64, signature_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
            signature = _b64url_decode(signature_b64)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LicenceInvalidError("licence token could not be parsed") from exc
        if header.get("alg") != "RS256":
            raise LicenceInvalidError(f"unsupported JWS alg: {header.get('alg')}")
        # Detached-JWS signing input: header.payload. (trailing dot, empty sig
        # segment is not part of the signed bytes).
        signing_input = f"{header_b64}.{payload_b64}.".encode()
        try:
            self._public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise LicenceInvalidError("licence signature is invalid") from exc
        return LicenceClaims.model_validate(payload)

    def evaluate(self, claims: LicenceClaims, *, now: int | None = None) -> LicenceState:
        """Evaluate entitlements, expiry, grace, and blocking for ``claims``."""
        now = now if now is not None else int(time.time())
        expired = now >= claims.not_after
        not_yet_valid = now < claims.not_before
        grace_end = claims.not_after + self._grace_days * _SECONDS_PER_DAY
        in_grace = expired and now < grace_end
        grace_days_left = (
            max(0, (grace_end - now) // _SECONDS_PER_DAY) if expired else self._grace_days
        )

        if not_yet_valid:
            # Not yet active — no entitlements, block nothing yet.
            return LicenceState(valid=False, expired=False, in_grace=False, grace_days_left=0)

        if expired and not in_grace:
            # After grace — disable ingest and AI; viewer and signing remain.
            logger.warning("LICENCE_EXPIRED after grace — ingest and AI disabled")
            return LicenceState(
                valid=False,
                site_id=claims.site_id,
                tier=claims.tier,
                seats=claims.seats,
                features=list(claims.features),
                not_after=claims.not_after,
                expired=True,
                in_grace=False,
                grace_days_left=0,
                blocking_capabilities=sorted(cap.value for cap in _LICENCE_BLOCKED),
            )

        if expired and in_grace:
            logger.warning("LICENCE_EXPIRED — within grace period; reading/signing continue")

        return LicenceState(
            valid=True,
            site_id=claims.site_id,
            tier=claims.tier,
            seats=claims.seats,
            features=list(claims.features),
            not_after=claims.not_after,
            expired=expired,
            in_grace=in_grace,
            grace_days_left=int(grace_days_left),
        )

    def has_feature(self, claims: LicenceClaims, feature: str) -> bool:
        """Look up a feature entitlement in the verified claims."""
        return feature in claims.features

    async def install(self, token: str) -> LicenceClaims:
        """Verify and persist a licence token issued by ``POST /admin/licence``."""
        claims = self.verify_token(token)
        await self._store.set(token)
        return claims

    async def current_state(self, *, now: int | None = None) -> LicenceState:
        """Return the evaluated state of the currently installed licence."""
        token = await self._store.get()
        if token is None:
            return LicenceState(valid=False)
        claims = self.verify_token(token)
        return self.evaluate(claims, now=now)
