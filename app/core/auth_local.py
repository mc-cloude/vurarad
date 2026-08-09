"""On-prem authentication — ``LocalOidcVerifier`` and the shared MFA contract.

The on-prem tier cannot depend on Google Identity Platform (no outbound
internet — criterion 5).  Instead it verifies OIDC ID tokens issued by a local
provider (e.g. Keycloak) using keys provisioned on disk, and enforces MFA from
the token claims.

MFA enforcement is **identical** to Identity Platform because both verifiers
route through one shared claim contract:
:func:`claims_to_second_factor_state` — the single source of truth for what
``amr`` / ``mfa_enrolled`` / ``mfa_time`` mean.  The unit suite
``tests/unit/test_local_oidc.py`` parametrises the *same* MFA assertions over
``LocalOidcVerifier`` and a reference Identity-Platform verifier, asserting
byte-identical ``SecondFactorState`` for every claim shape.

Signature verification uses only ``cryptography`` (already a dependency) so the
verifier runs fully offline — RS256 via a locally-provisioned JWKS, or HS256
via a shared secret.  No JWKS is fetched over the network at runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.auth import AuthenticatedUser, SecondFactorState
from app.core.capabilities import Role

# The Authentication Methods Reference value that indicates a second factor was
# used in this session.  Identity Platform and standards-conformant OIDC
# providers both set ``amr`` to include ``"mfa"`` on a multi-factor sign-in.
MFA_AMR_VALUE = "mfa"

# Default MFA verification window — mirrors ``Settings.mfa_verification_seconds``.
DEFAULT_MFA_WINDOW_SECONDS = 300


class OIDCVerificationError(Exception):
    """Raised when an ID token fails verification (signature, claims, expiry)."""


# ---------------------------------------------------------------------------
# Shared MFA claim contract — the Identity-Platform semantics, in one place.
# ---------------------------------------------------------------------------
def _amr_has_mfa(claims: Mapping[str, Any]) -> bool:
    amr = claims.get("amr")
    return isinstance(amr, list) and MFA_AMR_VALUE in amr


def claims_to_second_factor_state(
    claims: Mapping[str, Any],
    *,
    now: float,
    mfa_window_seconds: int = DEFAULT_MFA_WINDOW_SECONDS,
) -> SecondFactorState:
    """Map OIDC/Identity-Platform claims to a :class:`SecondFactorState`.

    Contract (honoured identically by the cloud and on-prem verifiers):

    - ``amr`` contains ``"mfa"`` → a second factor was used this session.
    - ``mfa_enrolled`` is ``True`` (or ``amr`` carries ``"mfa"``) → the user is
      enrolled in MFA.
    - ``mfa_time`` (falling back to ``auth_time``) is the second-factor
      assertion instant; if it is older than ``mfa_window_seconds`` the
      assertion is ``EXPIRED``.

    Result:
    - not enrolled and no second factor → ``UNENROLLED``
    - enrolled but no second factor this session → ``ENROLLED``
    - second factor present but stale → ``EXPIRED``
    - second factor present and fresh → ``VERIFIED``
    """
    has_mfa = _amr_has_mfa(claims)
    enrolled = bool(claims.get("mfa_enrolled", False)) or has_mfa
    if not enrolled:
        return SecondFactorState.UNENROLLED
    if not has_mfa:
        return SecondFactorState.ENROLLED
    mfa_time = claims.get("mfa_time", claims.get("auth_time"))
    if mfa_time is not None and (now - float(mfa_time)) > mfa_window_seconds:
        return SecondFactorState.EXPIRED
    return SecondFactorState.VERIFIED


def _map_role(claims: Mapping[str, Any]) -> Role:
    """Resolve the role claim to a :class:`Role`.

    The local OIDC provider must issue a ``role`` claim (one of the
    :class:`Role` values).  A missing or unknown role is a verification failure
    — the caller surfaces it as 401 TOKEN_INVALID.
    """
    role_claim = claims.get("role")
    if role_claim is None:
        raise OIDCVerificationError("token missing required 'role' claim")
    try:
        return Role(str(role_claim))
    except ValueError as exc:
        raise OIDCVerificationError(f"unknown role claim: {role_claim!r}") from exc


def claims_to_authenticated_user(
    claims: Mapping[str, Any],
    *,
    mfa_window_seconds: int = DEFAULT_MFA_WINDOW_SECONDS,
    now: float | None = None,
) -> AuthenticatedUser:
    """Build an :class:`AuthenticatedUser` from verified claims.

    Shared by ``LocalOidcVerifier`` and the reference Identity-Platform
    verifier, which is what makes their MFA enforcement identical.
    """
    if now is None:
        now = time.time()
    return AuthenticatedUser(
        uid=str(claims.get("sub", "")),
        email=claims.get("email"),
        role=_map_role(claims),
        display_name=claims.get("name"),
        mfa_state=claims_to_second_factor_state(
            claims, now=now, mfa_window_seconds=mfa_window_seconds
        ),
        operator_id=str(claims.get("operator_id", "")),
        tenant_id=str(claims.get("tenant_id", claims.get("tenant", "default"))),
    )


# ---------------------------------------------------------------------------
# JWT decode + verify (offline — RS256 via JWKS, or HS256 via shared secret)
# ---------------------------------------------------------------------------
def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url string, padding to a multiple of 4 as needed."""
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _signing_input(header_b64: str, payload_b64: str) -> bytes:
    return f"{header_b64}.{payload_b64}".encode("ascii")


def _rsa_public_key_from_jwk(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    """Build an RSA public key from a JWK (``n`` + ``e``, base64url-encoded)."""
    n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
    e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def decode_and_verify_jwt(
    token: str,
    *,
    issuer: str,
    audience: str,
    jwks: Mapping[str, Any] | None = None,
    hmac_secret: str | bytes | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Decode and verify an OIDC ID token offline.

    RS256 when ``jwks`` is given (key selected by ``kid``); HS256 when
    ``hmac_secret`` is given.  Validates ``iss``, ``aud``, ``exp`` (and
    ``nbf`` when present).  Returns the verified claims as a dict.
    """
    if now is None:
        now = time.time()
    if jwks is None and hmac_secret is None:
        raise OIDCVerificationError("no verification key configured (jwks or hmac_secret)")

    parts = token.split(".")
    if len(parts) != 3:
        raise OIDCVerificationError("malformed JWT: expected three segments")
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
        claims: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise OIDCVerificationError("malformed JWT header or payload") from exc

    alg = header.get("alg")
    signing_input = _signing_input(header_b64, payload_b64)
    signature = _b64url_decode(signature_b64)

    if alg == "RS256":
        if jwks is None:
            raise OIDCVerificationError("RS256 token but no JWKS configured")
        _verify_rs256(header, jwks, signing_input, signature)
    elif alg == "HS256":
        if hmac_secret is None:
            raise OIDCVerificationError("HS256 token but no hmac_secret configured")
        _verify_hs256(hmac_secret, signing_input, signature)
    else:
        raise OIDCVerificationError(f"unsupported JWT alg: {alg!r}")

    # Claim checks
    if claims.get("iss") != issuer:
        raise OIDCVerificationError(
            f"invalid issuer: expected {issuer!r}, got {claims.get('iss')!r}"
        )
    token_aud = claims.get("aud")
    aud_ok = token_aud == audience or (
        isinstance(token_aud, list) and audience in token_aud
    )
    if not aud_ok:
        raise OIDCVerificationError("invalid audience")
    exp = claims.get("exp")
    if exp is not None and float(exp) < now:
        raise OIDCVerificationError("token expired")
    nbf = claims.get("nbf")
    if nbf is not None and float(nbf) > now:
        raise OIDCVerificationError("token not yet valid (nbf)")

    return claims


def _verify_rs256(
    header: Mapping[str, Any],
    jwks: Mapping[str, Any],
    signing_input: bytes,
    signature: bytes,
) -> None:
    kid = header.get("kid")
    keys = jwks.get("keys", [])
    jwk = next((k for k in keys if k.get("kid") == kid), None)
    if jwk is None:
        raise OIDCVerificationError(f"no JWKS key for kid={kid!r}")
    public_key = _rsa_public_key_from_jwk(jwk)
    try:
        public_key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise OIDCVerificationError("invalid RS256 signature") from exc


def _verify_hs256(
    secret: str | bytes,
    signing_input: bytes,
    signature: bytes,
) -> None:
    key_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise OIDCVerificationError("invalid HS256 signature")


# ---------------------------------------------------------------------------
# LocalOidcVerifier — implements the TokenVerifier protocol for on-prem.
# ---------------------------------------------------------------------------
class LocalOidcVerifier:
    """Verify OIDC ID tokens issued by a local provider (on-prem tier).

    Implements the :class:`~app.core.auth.TokenVerifier` protocol.  MFA is
    enforced from the token claims via the shared
    :func:`claims_to_authenticated_user` contract, identical to Identity
    Platform.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: Mapping[str, Any] | None = None,
        hmac_secret: str | bytes | None = None,
        mfa_window_seconds: int = DEFAULT_MFA_WINDOW_SECONDS,
    ) -> None:
        if jwks is None and hmac_secret is None:
            raise ValueError("LocalOidcVerifier requires jwks or hmac_secret")
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks
        self._hmac_secret = hmac_secret
        self._mfa_window_seconds = mfa_window_seconds

    @classmethod
    def from_settings(cls, settings: Any) -> LocalOidcVerifier:
        """Build a verifier from application settings (on-prem tier).

        Loads the JWKS from ``settings.oidc_jwks_path`` when set (RS256);
        falls back to ``settings.oidc_hmac_secret`` for HS256.
        """
        jwks: Mapping[str, Any] | None = None
        jwks_path = settings.oidc_jwks_path
        if jwks_path:
            with open(jwks_path) as fh:
                jwks = json.load(fh)
        return cls(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks=jwks,
            hmac_secret=settings.oidc_hmac_secret,
            mfa_window_seconds=settings.mfa_verification_seconds,
        )

    async def verify(self, id_token: str) -> AuthenticatedUser:
        """Verify the bearer token and return the authenticated user."""
        claims = decode_and_verify_jwt(
            id_token,
            issuer=self._issuer,
            audience=self._audience,
            jwks=self._jwks,
            hmac_secret=self._hmac_secret,
        )
        return claims_to_authenticated_user(
            claims, mfa_window_seconds=self._mfa_window_seconds
        )
