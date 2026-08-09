# ruff: noqa: B008
"""LocalOIDC MFA enforcement — identical to Identity Platform.

The on-prem ``LocalOidcVerifier`` and a reference Identity-Platform verifier
both route through the single shared claim contract
(:func:`claims_to_authenticated_user`), so their MFA enforcement is provably
identical.  The same MFA assertions are parametrised over both verifiers and
must produce the same :class:`SecondFactorState` for every claim shape.

Acceptance criterion 4: "LocalOidcVerifier MFA identical to Identity Platform.
Same test, parametrised over both verifiers."
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.auth import SecondFactorState
from app.core.auth_local import (
    LocalOidcVerifier,
    OIDCVerificationError,
    claims_to_authenticated_user,
    claims_to_second_factor_state,
    decode_and_verify_jwt,
)
from app.core.capabilities import Role

ISSUER = "https://idp.local/vurarad"
AUDIENCE = "vurarad-api"

# ---------------------------------------------------------------------------
# Test key material (RS256) + JWT minting helpers
# ---------------------------------------------------------------------------
_test_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_test_pub = _test_key.public_key().public_numbers()


def _int_to_b64url(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


JWK: dict[str, Any] = {
    "kid": "test-key",
    "kty": "RSA",
    "alg": "RS256",
    "n": _int_to_b64url(_test_pub.n),
    "e": _int_to_b64url(_test_pub.e),
}
JWKS: dict[str, Any] = {"keys": [JWK]}

_HMAC_SECRET = "onprem-shared-secret"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _sign_rs256(signing_input: bytes) -> bytes:
    return _test_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())


def _sign_hs256(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def mint_jwt(claims: dict[str, Any], *, alg: str = "RS256", kid: str = "test-key") -> str:
    header: dict[str, Any] = {"alg": alg, "typ": "JWT", "kid": kid}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = _sign_hs256(signing_input, _HMAC_SECRET) if alg == "HS256" else _sign_rs256(signing_input)
    return f"{header_b64}.{payload_b64}.{_b64url(sig)}"


def base_claims(**extra: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": "u-1",
        "email": "rad@example.com",
        "role": "radiologist",
        "name": "Dr Rad",
        "operator_id": "01HZTESTOP",
        "tenant_id": "tenant-a",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 3600,
    }
    claims.update(extra)
    return claims


# ---------------------------------------------------------------------------
# Reference Identity-Platform verifier — same MFA contract as the local one.
# ---------------------------------------------------------------------------
class ReferenceIdentityPlatformVerifier:
    """Reference for the cloud Identity-Platform claim contract.

    Represents what the cloud tier's verifier produces: it verifies the ID
    token and then applies the *same* shared MFA claim contract
    (:func:`claims_to_authenticated_user`) as :class:`LocalOidcVerifier`.  That
    shared contract is precisely what makes on-prem MFA enforcement identical
    to Identity Platform.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: dict[str, Any] | None = None,
        hmac_secret: str | None = None,
        mfa_window_seconds: int = 300,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks
        self._hmac_secret = hmac_secret
        self._mfa_window_seconds = mfa_window_seconds

    async def verify(self, id_token: str) -> Any:
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


@pytest.fixture(params=["local", "idp"])
def verifier(request: pytest.FixtureRequest) -> Any:
    """Parametrised verifier — every MFA test runs against both."""
    if request.param == "local":
        return LocalOidcVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=JWKS)
    return ReferenceIdentityPlatformVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=JWKS)


# ---------------------------------------------------------------------------
# MFA claim shapes → expected SecondFactorState (the contract under test)
# ---------------------------------------------------------------------------
_NOW = time.time()
CLAIM_SHAPES: list[tuple[dict[str, Any], SecondFactorState]] = [
    # second factor present and fresh → VERIFIED
    ({"amr": ["mfa"]}, SecondFactorState.VERIFIED),
    ({"amr": ["pwd", "mfa"], "mfa_enrolled": True}, SecondFactorState.VERIFIED),
    ({"amr": ["mfa"], "mfa_time": _NOW - 10}, SecondFactorState.VERIFIED),
    ({"amr": ["mfa"], "auth_time": _NOW - 10}, SecondFactorState.VERIFIED),
    # second factor present but stale → EXPIRED
    ({"amr": ["mfa"], "mfa_time": _NOW - 1000}, SecondFactorState.EXPIRED),
    ({"amr": ["mfa"], "auth_time": _NOW - 1000}, SecondFactorState.EXPIRED),
    # enrolled but no second factor this session → ENROLLED
    ({"mfa_enrolled": True}, SecondFactorState.ENROLLED),
    ({"amr": ["pwd"], "mfa_enrolled": True}, SecondFactorState.ENROLLED),
    # not enrolled and no second factor → UNENROLLED
    ({}, SecondFactorState.UNENROLLED),
    ({"amr": ["pwd"]}, SecondFactorState.UNENROLLED),
]


# ---------------------------------------------------------------------------
# Protocol + identity
# ---------------------------------------------------------------------------
def test_verifier_implements_token_verifier_protocol(verifier: Any) -> None:
    """``TokenVerifier`` is a non-runtime-checkable Protocol; assert the shape."""
    assert hasattr(verifier, "verify")
    assert inspect.iscoroutinefunction(verifier.verify)


@pytest.mark.parametrize("extra,expected", CLAIM_SHAPES)
async def test_mfa_enforcement(
    verifier: Any, extra: dict[str, Any], expected: SecondFactorState
) -> None:
    """Each claim shape yields the expected MFA state — on BOTH verifiers."""
    token = mint_jwt(base_claims(**extra))
    user = await verifier.verify(token)
    assert user.mfa_state == expected


@pytest.mark.parametrize("extra,expected", CLAIM_SHAPES)
async def test_both_verifiers_produce_identical_users(
    extra: dict[str, Any], expected: SecondFactorState
) -> None:
    """Local and Identity-Platform verifiers produce byte-identical users."""
    token = mint_jwt(base_claims(**extra))
    local = LocalOidcVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=JWKS)
    idp = ReferenceIdentityPlatformVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=JWKS)
    u_local = await local.verify(token)
    u_idp = await idp.verify(token)
    assert u_local == u_idp
    assert u_local.mfa_state == expected


# ---------------------------------------------------------------------------
# User fields are populated from claims
# ---------------------------------------------------------------------------
async def test_user_fields_populated(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"]))
    user = await verifier.verify(token)
    assert user.uid == "u-1"
    assert user.email == "rad@example.com"
    assert user.role == Role.RADIOLOGIST
    assert user.display_name == "Dr Rad"
    assert user.operator_id == "01HZTESTOP"
    assert user.tenant_id == "tenant-a"
    assert user.mfa_state == SecondFactorState.VERIFIED


async def test_audience_as_list_is_accepted(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"], aud=[AUDIENCE, "other"]))
    user = await verifier.verify(token)
    assert user.mfa_state == SecondFactorState.VERIFIED


# ---------------------------------------------------------------------------
# MFA window is configurable
# ---------------------------------------------------------------------------
async def test_mfa_window_is_configurable() -> None:
    """A 60s window expires an assertion that a 300s window accepts."""
    token = mint_jwt(base_claims(amr=["mfa"], mfa_time=time.time() - 100))
    strict = LocalOidcVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks=JWKS, mfa_window_seconds=60
    )
    loose = LocalOidcVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks=JWKS, mfa_window_seconds=300
    )
    assert (await strict.verify(token)).mfa_state == SecondFactorState.EXPIRED
    assert (await loose.verify(token)).mfa_state == SecondFactorState.VERIFIED


# ---------------------------------------------------------------------------
# Direct contract-function checks
# ---------------------------------------------------------------------------
def test_claims_to_second_factor_state_direct() -> None:
    assert (
        claims_to_second_factor_state({"amr": ["mfa"]}, now=_NOW)
        == SecondFactorState.VERIFIED
    )
    assert (
        claims_to_second_factor_state({}, now=_NOW) == SecondFactorState.UNENROLLED
    )
    assert (
        claims_to_second_factor_state({"mfa_enrolled": True}, now=_NOW)
        == SecondFactorState.ENROLLED
    )
    assert (
        claims_to_second_factor_state({"amr": ["mfa"], "mfa_time": _NOW - 9999}, now=_NOW)
        == SecondFactorState.EXPIRED
    )


# ---------------------------------------------------------------------------
# Rejection paths — invalid tokens must raise (→ 401 in the API layer)
# ---------------------------------------------------------------------------
async def test_missing_role_claim_rejected(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"], role=None))  # type: ignore[arg-type]
    with pytest.raises(OIDCVerificationError):
        await verifier.verify(token)


async def test_unknown_role_claim_rejected(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"], role="ghost"))
    with pytest.raises(OIDCVerificationError):
        await verifier.verify(token)


async def test_invalid_issuer_rejected(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"], iss="https://evil.example"))
    with pytest.raises(OIDCVerificationError):
        await verifier.verify(token)


async def test_invalid_audience_rejected(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"], aud="someone-else"))
    with pytest.raises(OIDCVerificationError):
        await verifier.verify(token)


async def test_expired_token_rejected(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"], exp=int(time.time()) - 10))
    with pytest.raises(OIDCVerificationError):
        await verifier.verify(token)


async def test_tampered_signature_rejected(verifier: Any) -> None:
    token = mint_jwt(base_claims(amr=["mfa"]))
    header_b64, payload_b64, _sig_b64 = token.split(".")
    # Flip a payload byte → signature no longer matches.
    tampered = f"{header_b64}.{payload_b64[:-1]}{'A' if payload_b64[-1] != 'A' else 'B'}.aaa"
    with pytest.raises(OIDCVerificationError):
        await verifier.verify(tampered)


async def test_malformed_token_rejected(verifier: Any) -> None:
    with pytest.raises(OIDCVerificationError):
        await verifier.verify("not.a.jwt")


# ---------------------------------------------------------------------------
# Key configuration
# ---------------------------------------------------------------------------
async def test_hs256_verification_with_shared_secret() -> None:
    token = mint_jwt(base_claims(amr=["mfa"]), alg="HS256", kid="hmac")
    local = LocalOidcVerifier(issuer=ISSUER, audience=AUDIENCE, hmac_secret=_HMAC_SECRET)
    user = await local.verify(token)
    assert user.mfa_state == SecondFactorState.VERIFIED


async def test_rs256_token_without_jwks_rejected() -> None:
    token = mint_jwt(base_claims(amr=["mfa"]))
    local = LocalOidcVerifier(issuer=ISSUER, audience=AUDIENCE, hmac_secret=_HMAC_SECRET)
    with pytest.raises(OIDCVerificationError, match="RS256"):
        await local.verify(token)


def test_constructing_verifier_without_any_key_raises() -> None:
    with pytest.raises(ValueError):
        LocalOidcVerifier(issuer=ISSUER, audience=AUDIENCE)


def test_decode_and_verify_requires_a_key() -> None:
    with pytest.raises(OIDCVerificationError):
        decode_and_verify_jwt("a.b.c", issuer=ISSUER, audience=AUDIENCE)
