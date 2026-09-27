"""The session thread, reconnect backoff and request pacing (roadmap 19.2).

``ib_async`` is asyncio based. The tick is synchronous. :class:`LoopThread`
runs one event loop on its own daemon thread per gateway session and gives
a blocking facade: :meth:`LoopThread.run` waits for a coroutine,
:meth:`LoopThread.call` runs a plain function on the loop thread (the
vendor objects are only touched there).

:func:`retry_until` reconnects with exponential backoff and jitter, capped
at 60 seconds between tries, until a deadline. :class:`TokenBucket` paces
requests per gateway so a burst of sync or health calls never starves the
submit path (IBKR allows about 50 messages a second).

Nothing here knows IBKR, so it is tested without a gateway.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import random
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class LoopThread:
    """One asyncio event loop on a daemon thread."""

    def __init__(self, name: str = "ibkr-session") -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self.loop.is_closed()

    def run[T](self, factory: Callable[[], Awaitable[T]], timeout: float) -> T:
        """Run the awaitable ``factory`` makes on the loop and wait for it.
        Raises ``TimeoutError`` (and cancels it) after ``timeout`` seconds."""

        async def _wrapped() -> T:
            return await factory()

        future = asyncio.run_coroutine_threadsafe(_wrapped(), self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"no answer in {timeout:g} s") from None

    def call[T](self, fn: Callable[[], T], timeout: float) -> T:
        """Run the plain function ``fn`` on the loop thread and wait."""

        async def _call() -> T:
            return fn()

        return self.run(_call, timeout)

    def stop(self) -> None:
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self.loop.close()


@dataclass(frozen=True)
class Backoff:
    """Exponential backoff with jitter: ``base * 2**attempt``, capped, then
    shortened by up to ``jitter`` of itself so many clients spread out."""

    base: float = 1.0
    cap: float = 60.0
    jitter: float = 0.2

    def delay(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        raw = min(self.cap, self.base * (2 ** max(0, attempt)))
        return raw * (1.0 - self.jitter * rand())


def retry_until[T](
    fn: Callable[[], T],
    *,
    deadline_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    backoff: Backoff | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    """Call ``fn`` until it succeeds. A ``retry_on`` error waits the backoff
    and tries again while the next try would start before the deadline;
    after that the last error is raised."""
    backoff = backoff or Backoff()
    start = monotonic()
    attempt = 0
    while True:
        try:
            return fn()
        except retry_on as exc:
            wait = backoff.delay(attempt, rand)
            if monotonic() + wait - start > deadline_seconds:
                raise
            if on_retry is not None:
                on_retry(attempt + 1, wait, exc)
            sleep(wait)
            attempt += 1


class TokenBucket:
    """At most ``rate`` requests a second on average, bursts up to
    ``capacity``. :meth:`acquire` blocks until a token is free."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._tokens = self.capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                self._sleep((1.0 - self._tokens) / self.rate)
