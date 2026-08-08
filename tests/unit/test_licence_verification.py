"""WP14 criterion 7 — offline detached-JWS licence verification, grace, entitlements.

The licence is a detached JWS (RFC 7515) signed by an offline key and verified
against ``licence_public_key_pem`` with no outbound call and no shared secret.
Expiry starts a ``licence_grace_days`` grace period; after grace, ingest and AI
disable but the viewer and report signing keep working.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.errors import LicenceInvalidError
from app.services.licence_service import (
    LicenceClaims,
    LicenceService,
    LicenceStore,
)

DAY = 86400
NOW = 1_700_000_000  # fixed timestamp for deterministic grace maths


# ---------------------------------------------------------------------------
# JWS helpers — produce tokens the service can verify (RS256 compact form)
# ---------------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_keypair() -> tuple[rsa.RSAPrivateKey, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private, public_pem


def _sign(claims: dict[str, object], private_key: rsa.RSAPrivateKey) -> str:
    header = {"alg": "RS256", "typ": "JWS"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}.".encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _claims(
    *,
    site_id: str = "site-1",
    seats: int = 5,
    tier: str = "P2",
    features: list[str] | None = None,
    not_before: int = NOW - 100,
    not_after: int = NOW + 30 * DAY,
) -> dict[str, object]:
    return {
        "siteId": site_id,
        "seats": seats,
        "tier": tier,
        "features": features if features is not None else ["findings_ingest", "workbench"],
        "notBefore": not_before,
        "notAfter": not_after,
    }


# ---------------------------------------------------------------------------
# In-memory licence store
# ---------------------------------------------------------------------------
class InMemoryLicenceStore:
    def __init__(self) -> None:
        self._token: str | None = None

    async def get(self) -> str | None:
        return self._token

    async def set(self, token: str) -> None:
        self._token = token


def _service(public_pem: str | None, *, grace_days: int = 30) -> LicenceService:
    return LicenceService(InMemoryLicenceStore(), public_key_pem=public_pem, grace_days=grace_days)


# ---------------------------------------------------------------------------
# Offline verification (criterion 7) — no network, public key, no shared secret
# ---------------------------------------------------------------------------
def test_verify_valid_licence_offline() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(), private)
    svc = _service(public_pem)
    claims = svc.verify_token(token)
    assert claims.site_id == "site-1"
    assert claims.seats == 5
    assert claims.tier == "P2"
    assert "findings_ingest" in claims.features


def test_verify_is_offline_with_only_public_key() -> None:
    """Verification needs only the public key — no private key, no network call."""
    private, public_pem = _make_keypair()
    token = _sign(_claims(), private)
    svc = _service(public_pem)
    # The private key is not retained by the service; verification succeeds
    # with the public key alone (asymmetric, detached).
    claims = svc.verify_token(token)
    assert claims.site_id == "site-1"


def test_tampered_signature_rejected() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(), private)
    # Flip the first character of the signature segment.  The first base64url
    # char always encodes real signature bits (unlike the last char, whose
    # lower bits may be zero-padding for a non-multiple-of-3 length — flipping
    # only padding bits leaves the decoded bytes identical, causing a flaky
    # pass).
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
    svc = _service(public_pem)
    with pytest.raises(LicenceInvalidError, match="signature"):
        svc.verify_token(tampered)


def test_tampered_payload_rejected() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(seats=5), private)
    # Re-sign with a different private key but keep the original signature →
    # the payload no longer matches the signature.
    other_private, _ = _make_keypair()
    forged = _sign(_claims(seats=999), other_private)
    head, _, sig = token.split(".")
    _, payload, _ = forged.split(".")
    mismatched = f"{head}.{payload}.{sig}"
    svc = _service(public_pem)
    with pytest.raises(LicenceInvalidError):
        svc.verify_token(mismatched)


def test_wrong_key_rejected() -> None:
    private, _ = _make_keypair()
    other_private, other_public_pem = _make_keypair()
    token = _sign(_claims(), private)
    svc = _service(other_public_pem)
    with pytest.raises(LicenceInvalidError, match="signature"):
        svc.verify_token(token)


def test_malformed_token_rejected() -> None:
    _, public_pem = _make_keypair()
    svc = _service(public_pem)
    with pytest.raises(LicenceInvalidError, match="JWS"):
        svc.verify_token("not-a-jws")


def test_unsupported_alg_rejected() -> None:
    private, public_pem = _make_keypair()
    header = {"alg": "HS256", "typ": "JWS"}  # shared-secret alg — must be rejected
    header_b64 = _b64url(json.dumps(header).encode())
    payload_b64 = _b64url(json.dumps(_claims()).encode())
    signing_input = f"{header_b64}.{payload_b64}.".encode()
    sig = private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    token = f"{header_b64}.{payload_b64}.{_b64url(sig)}"
    svc = _service(public_pem)
    with pytest.raises(LicenceInvalidError, match="alg"):
        svc.verify_token(token)


def test_no_key_configured_rejected() -> None:
    svc = _service(None)
    with pytest.raises(LicenceInvalidError, match="not configured"):
        svc.verify_token("anything")


def test_non_rsa_key_rejected() -> None:
    # An Ed25519 key is not an RSA key — the loader must refuse it.
    from cryptography.hazmat.primitives.asymmetric import ed25519

    ed_key = ed25519.Ed25519PrivateKey.generate()
    bad_pem = ed_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    with pytest.raises(TypeError):
        _service(bad_pem)


# ---------------------------------------------------------------------------
# Grace period (criterion 7) — expiry starts grace; after grace, partial block
# ---------------------------------------------------------------------------
def test_valid_licence_state_blocks_nothing() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(not_after=NOW + 30 * DAY), private)
    svc = _service(public_pem)
    claims = svc.verify_token(token)
    state = svc.evaluate(claims, now=NOW)
    assert state.valid is True
    assert state.expired is False
    assert state.in_grace is False
    assert state.blocking_capabilities == []


def test_expired_within_grace_blocks_nothing() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(not_after=NOW - 5 * DAY), private)
    svc = _service(public_pem, grace_days=30)
    claims = svc.verify_token(token)
    state = svc.evaluate(claims, now=NOW)
    assert state.expired is True
    assert state.in_grace is True
    assert state.valid is True  # still operational within grace
    assert state.grace_days_left > 0
    # Reading and signing are never blocked during grace.
    assert state.blocking_capabilities == []


def test_expired_after_grace_disables_ingest_and_ai_only() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(not_after=NOW - 40 * DAY), private)
    svc = _service(public_pem, grace_days=30)
    claims = svc.verify_token(token)
    state = svc.evaluate(claims, now=NOW)
    assert state.expired is True
    assert state.in_grace is False
    assert state.valid is False
    blocked = set(state.blocking_capabilities)
    # Ingest and AI are disabled…
    assert "study:import" in blocked
    assert "study:write" in blocked
    assert "ai:use" in blocked
    assert "research:draft" in blocked
    # …but the viewer and report signing keep working (criterion 7).
    assert "study:read" not in blocked
    assert "report:write" not in blocked
    assert "report:sign" not in blocked


def test_not_yet_valid_is_not_entitled() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(not_before=NOW + 10 * DAY, not_after=NOW + 40 * DAY), private)
    svc = _service(public_pem)
    claims = svc.verify_token(token)
    state = svc.evaluate(claims, now=NOW)
    assert state.valid is False


def test_grace_days_left_counts_down() -> None:
    private, public_pem = _make_keypair()
    # Expired 10 days ago, 30-day grace → ~20 days left.
    token = _sign(_claims(not_after=NOW - 10 * DAY), private)
    svc = _service(public_pem, grace_days=30)
    claims = svc.verify_token(token)
    state = svc.evaluate(claims, now=NOW)
    assert state.in_grace is True
    assert 19 <= state.grace_days_left <= 20


# ---------------------------------------------------------------------------
# Feature entitlements (criterion 7)
# ---------------------------------------------------------------------------
def test_feature_entitlement_lookup() -> None:
    private, public_pem = _make_keypair()
    token = _sign(
        _claims(features=["findings_ingest", "slicer_addon", "workbench"]), private
    )
    svc = _service(public_pem)
    claims = svc.verify_token(token)
    assert svc.has_feature(claims, "findings_ingest") is True
    assert svc.has_feature(claims, "slicer_addon") is True
    assert svc.has_feature(claims, "workbench") is True
    assert svc.has_feature(claims, "enterprise_sso") is False


def test_tier_and_seats_carried_in_state() -> None:
    private, public_pem = _make_keypair()
    token = _sign(_claims(seats=12, tier="ENTERPRISE"), private)
    svc = _service(public_pem)
    claims = svc.verify_token(token)
    state = svc.evaluate(claims, now=NOW)
    assert state.tier == "ENTERPRISE"
    assert state.seats == 12
    assert state.site_id == "site-1"


# ---------------------------------------------------------------------------
# Install + current_state (the route flow)
# ---------------------------------------------------------------------------
async def test_install_and_current_state() -> None:
    private, public_pem = _make_keypair()
    store = InMemoryLicenceStore()
    svc = LicenceService(store, public_key_pem=public_pem, grace_days=30)
    token = _sign(_claims(), private)
    claims = await svc.install(token)
    assert claims.site_id == "site-1"

    state = await svc.current_state(now=NOW)
    assert state.valid is True
    assert state.site_id == "site-1"


async def test_current_state_when_no_licence_installed() -> None:
    svc = _service(_make_keypair()[1])
    state = await svc.current_state(now=NOW)
    assert state.valid is False
    assert state.site_id is None


async def test_install_rejects_invalid_token_without_storing() -> None:
    store = InMemoryLicenceStore()
    svc = LicenceService(store, public_key_pem=_make_keypair()[1], grace_days=30)
    with pytest.raises(LicenceInvalidError):
        await svc.install("garbage")
    # Nothing was stored.
    assert await store.get() is None


# ---------------------------------------------------------------------------
# Protocol sanity
# ---------------------------------------------------------------------------
def test_in_memory_store_satisfies_protocol() -> None:
    store: LicenceStore = InMemoryLicenceStore()
    assert hasattr(store, "get")
    assert hasattr(store, "set")


def test_claims_model_round_trips() -> None:
    claims = LicenceClaims(
        site_id="s", seats=1, tier="P1", features=["x"], not_before=0, not_after=1
    )
    dumped = claims.model_dump(by_alias=True)
    assert dumped["siteId"] == "s"
    assert dumped["notAfter"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
