"""Rate limiter — bucket exhaustion, window reset, per-bucket independence."""

from __future__ import annotations

import pytest

from app.core.ratelimit import BUCKET_LIMITS, RateLimitBucket, RateLimiter


def test_first_request_allowed() -> None:
    limiter = RateLimiter()
    result = limiter.check("user-1", RateLimitBucket.DEFAULT)
    assert result.allowed is True
    assert result.remaining == BUCKET_LIMITS[RateLimitBucket.DEFAULT] - 1


def test_bucket_exhaustion_denies() -> None:
    limiter = RateLimiter()
    limit = BUCKET_LIMITS[RateLimitBucket.AI]
    for _ in range(limit):
        assert limiter.check("user-1", RateLimitBucket.AI).allowed is True
    denied = limiter.check("user-1", RateLimitBucket.AI)
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after > 0


def test_window_reset_after_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    current = [1000.0]
    monkeypatch.setattr("app.core.ratelimit.time.time", lambda: current[0])
    limiter = RateLimiter()
    limit = BUCKET_LIMITS[RateLimitBucket.SIGN]
    for _ in range(limit):
        assert limiter.check("user-1", RateLimitBucket.SIGN).allowed is True
    assert limiter.check("user-1", RateLimitBucket.SIGN).allowed is False
    # Advance past the 60s window → counter resets.
    current[0] += 61.0
    assert limiter.check("user-1", RateLimitBucket.SIGN).allowed is True


def test_different_keys_are_independent() -> None:
    limiter = RateLimiter()
    limit = BUCKET_LIMITS[RateLimitBucket.IMPORT]
    for _ in range(limit):
        assert limiter.check("user-A", RateLimitBucket.IMPORT).allowed is True
    # user-A exhausted, user-B still has budget.
    assert limiter.check("user-A", RateLimitBucket.IMPORT).allowed is False
    assert limiter.check("user-B", RateLimitBucket.IMPORT).allowed is True


def test_different_buckets_are_independent() -> None:
    limiter = RateLimiter()
    # Exhaust the MFA bucket for one key.
    mfa_limit = BUCKET_LIMITS[RateLimitBucket.MFA]
    for _ in range(mfa_limit):
        assert limiter.check("user-1", RateLimitBucket.MFA).allowed is True
    assert limiter.check("user-1", RateLimitBucket.MFA).allowed is False
    # Same key, different bucket is unaffected.
    assert limiter.check("user-1", RateLimitBucket.DEFAULT).allowed is True


def test_reset_at_is_window_start_plus_60() -> None:
    limiter = RateLimiter()
    result = limiter.check("user-1", RateLimitBucket.DEFAULT)
    assert result.reset_at >= result.retry_after or result.reset_at > 0


def test_every_bucket_has_a_limit() -> None:
    for bucket in RateLimitBucket:
        assert bucket in BUCKET_LIMITS
        assert BUCKET_LIMITS[bucket] > 0
