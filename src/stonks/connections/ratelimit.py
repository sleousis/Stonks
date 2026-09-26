"""Per-provider token buckets, shared by every sync thread.

Each provider declares a :class:`~stonks.connections.base.RateLimit`: an
app-wide bucket (all connections together) and a per-connection bucket.
:meth:`RateLimiter.acquire` takes one token from both, waiting when that is
short; when the wait would exceed ``max_wait`` it raises
:class:`~stonks.connections.base.RateLimited` so the sync moves instead of
blocking a worker.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from stonks.connections.base import RateLimit, RateLimited


class _Bucket:
    def __init__(self, per_minute: int, now: float) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.tokens = self.capacity
        self.stamp = now

    def refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def wait_for_one(self) -> float:
        return 0.0 if self.tokens >= 1.0 else (1.0 - self.tokens) / self.rate


class RateLimiter:
    def __init__(
        self,
        limit: RateLimit,
        *,
        name: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limit = limit
        self.name = name
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._app = _Bucket(limit.per_minute, clock())
        self._per_connection: dict[str, _Bucket] = {}

    def acquire(self, connection_id: str, *, max_wait: float = 30.0) -> None:
        while True:
            with self._lock:
                now = self._clock()
                conn = self._per_connection.get(connection_id)
                if conn is None:
                    conn = self._per_connection[connection_id] = _Bucket(
                        self.limit.per_connection_per_minute, now
                    )
                self._app.refill(now)
                conn.refill(now)
                wait = max(self._app.wait_for_one(), conn.wait_for_one())
                if wait <= 0:
                    self._app.tokens -= 1.0
                    conn.tokens -= 1.0
                    return
            if wait > max_wait:
                raise RateLimited(
                    f"{self.name or 'provider'} rate limit reached; retry in {wait:.0f}s",
                    retry_after=wait,
                )
            self._sleep(wait)


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(provider: str, limit: RateLimit) -> RateLimiter:
    """The one limiter of ``provider`` in this process."""
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(provider)
        if limiter is None or limiter.limit != limit:
            limiter = _LIMITERS[provider] = RateLimiter(limit, name=provider)
        return limiter


def reset_limiters() -> None:
    """Forget every limiter (tests)."""
    with _LIMITERS_LOCK:
        _LIMITERS.clear()
