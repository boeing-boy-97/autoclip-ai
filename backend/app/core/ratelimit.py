"""Redis-backed request rate limiting.

Why a fixed window instead of a token bucket: the limits that matter here are coarse (tens per
hour for uploads, tens per minute for login) and a fixed window is one `INCR` on an expiring key —
cheap enough to run on every mutating request, and shared across API replicas because the counter
lives in Redis rather than in process memory.

Two behaviours are deliberate:

* **Fail open, loudly.** If Redis is unreachable the limiter logs a warning and allows the request.
  A limiter that turns a cache outage into a total outage is worse than no limiter; the cost of a
  few unmetered requests is bounded, the cost of a dead API is not.
* **Disabled means disabled.** A limit of ``0`` (or negative) turns the check off, which is how a
  deployment opts out (``RATE_LIMIT_UPLOAD_PER_HOUR=0``) without code changes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class CounterClient(Protocol):
    """The slice of the Redis API this module needs (fakeredis satisfies it too)."""

    def pipeline(self, transaction: bool = True) -> Any: ...


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int

    def as_headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(self.retry_after_seconds, 1))
        return headers


class FixedWindowLimiter:
    """Count requests per (scope, identity, window bucket) in Redis."""

    def __init__(self, client: CounterClient, *, prefix: str = "ratelimit") -> None:
        self.client = client
        self.prefix = prefix

    def _key(self, scope: str, identity: str, window_seconds: int, now: float) -> str:
        bucket = int(now) // max(window_seconds, 1)
        return f"{self.prefix}:{scope}:{identity}:{bucket}"

    def consume(
        self,
        *,
        scope: str,
        identity: str,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Count one request. Never raises: infrastructure failure allows the request."""
        if limit <= 0:
            return RateLimitDecision(allowed=True, limit=0, remaining=0, retry_after_seconds=0)

        key = self._key(scope, identity, window_seconds, now if now is not None else time.time())
        try:
            # MULTI/EXEC makes increment + expiry indivisible, including on client crashes.
            # Refreshing expiry is safe: the bucket is part of the key, never a sliding window.
            with self.client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds + 1)
                pipe.ttl(key)
                count, _expiry_set, ttl = pipe.execute()
            count, ttl = int(count), int(ttl)
        except Exception as exc:  # noqa: BLE001 - any Redis failure must not break the request
            logger.warning(
                "ratelimit.unavailable",
                extra={"scope": scope, "error": type(exc).__name__},
            )
            return RateLimitDecision(
                allowed=True, limit=limit, remaining=limit, retry_after_seconds=0
            )

        retry_after = ttl if ttl > 0 else window_seconds
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(limit - count, 0),
            retry_after_seconds=retry_after,
        )


__all__ = ["FixedWindowLimiter", "RateLimitDecision"]
