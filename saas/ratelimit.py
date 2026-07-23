"""Per-tenant request rate limiting over Redis counters (E2.5).

A fixed-window counter: for each tenant + wall-clock minute bucket we ``INCR`` a Redis
key (with a matching TTL) and reject once the count exceeds ``rate_limit_per_minute``.
Cheap, atomic, and shared across the API replicas that hit the same Redis.

Design notes / invariants:
- **Fail-open**: rate limiting is a guardrail, not a correctness feature — if Redis is
  unreachable (e.g. host-run tools with no broker) :meth:`RateLimiter.check` returns
  *allowed* and never raises. Availability of the design workflow beats strict limiting.
- Off the hot path for host tools / tests: the caller (the API auth dependency) only
  invokes this when auth is enabled; tests inject a ``fakeredis`` connection directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis import Redis

_PREFIX = "copilot:ratelimit"


@dataclass
class RateLimitResult:
    allowed: bool
    count: int
    limit: int
    retry_after: int


class RateLimiter:
    """Fixed-window per-tenant limiter backed by Redis ``INCR`` + ``EXPIRE``."""

    def __init__(
        self, connection: "Redis | None" = None, *, prefix: str = _PREFIX
    ) -> None:
        self._conn = connection
        self._prefix = prefix

    @property
    def connection(self) -> "Redis":
        if self._conn is None:
            # Reuse the queue's process-wide connection so all features share one client.
            from .queue import get_redis_connection

            self._conn = get_redis_connection()
        return self._conn

    def check(
        self, tenant_id: str, *, limit: int, window_s: int = 60
    ) -> RateLimitResult:
        """Count this request against the tenant's window and report the verdict.

        Best-effort: any Redis error fails open (``allowed=True``) so a broker hiccup
        never blocks a design workflow.
        """
        if limit <= 0:
            return RateLimitResult(allowed=True, count=0, limit=limit, retry_after=0)
        now = time.time()
        bucket = int(now // window_s)
        key = f"{self._prefix}:{tenant_id}:{bucket}"
        try:
            conn = self.connection
            count = int(conn.incr(key))
            if count == 1:
                # First hit in this window — set the TTL so the counter self-expires.
                conn.expire(key, window_s)
        except Exception:  # noqa: BLE001 - rate limiting is best-effort / fail-open
            return RateLimitResult(allowed=True, count=0, limit=limit, retry_after=0)
        retry_after = window_s - int(now % window_s)
        return RateLimitResult(
            allowed=count <= limit, count=count, limit=limit, retry_after=retry_after
        )


_LIMITER: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Process-wide rate limiter (lazily builds its Redis connection on first use)."""
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter()
    return _LIMITER


__all__ = ["RateLimitResult", "RateLimiter", "get_rate_limiter"]
