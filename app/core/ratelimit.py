"""Per-route rate limiting with Firestore-transactional counters.

Named buckets from the plan §3.0.3.  Each check reads the current minute's
counter and fails with 429 if the bucket is exhausted.
"""

import time
from dataclasses import dataclass
from enum import StrEnum


class RateLimitBucket(StrEnum):
    DEFAULT = "default"
    AI = "ai"
    SIGN = "sign"
    IMAGING = "imaging"
    SEARCH = "search"
    IMPORT = "import"
    EXPORT = "export"
    ADMIN = "admin"
    HEALTH = "health"
    AUTH = "auth"
    DICTATION = "dictation"
    MFA = "mfa"


# Requests per minute per bucket
BUCKET_LIMITS: dict[RateLimitBucket, int] = {
    RateLimitBucket.DEFAULT: 300,
    RateLimitBucket.AI: 20,
    RateLimitBucket.SIGN: 30,
    RateLimitBucket.IMAGING: 120,
    RateLimitBucket.SEARCH: 60,
    RateLimitBucket.IMPORT: 10,
    RateLimitBucket.EXPORT: 5,
    RateLimitBucket.ADMIN: 60,
    RateLimitBucket.HEALTH: 1000,
    RateLimitBucket.AUTH: 60,
    RateLimitBucket.DICTATION: 100,
    RateLimitBucket.MFA: 10,
}


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_at: float
    retry_after: float = 0.0


class RateLimiter:
    """Memory-backed rate limiter for single-process deployments.

    In production this would be backed by Firestore counters, but for WP1
    an in-memory implementation keeps the dependency graph small.
    """

    def __init__(self) -> None:
        self._windows: dict[str, tuple[int, float]] = {}

    def check(self, key: str, bucket: RateLimitBucket) -> RateLimitResult:
        now = time.time()
        limit = BUCKET_LIMITS[bucket]
        full_key = f"{bucket}:{key}"

        count, window_start = self._windows.get(full_key, (0, now))

        # Reset window if > 60s elapsed
        if now - window_start >= 60.0:
            count = 0
            window_start = now

        remaining = limit - count - 1
        if remaining < 0:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_at=window_start + 60.0,
                retry_after=window_start + 60.0 - now,
            )

        self._windows[full_key] = (count + 1, window_start)
        return RateLimitResult(
            allowed=True,
            remaining=remaining,
            reset_at=window_start + 60.0,
        )
