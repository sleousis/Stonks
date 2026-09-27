"""The IBKR session thread, backoff and pacing (roadmap 19.2)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from stonks.execution.brokers.ibkr.session import Backoff, LoopThread, TokenBucket, retry_until


def test_loop_thread_runs_coroutines_and_functions_on_its_thread():
    lt = LoopThread()
    try:

        async def answer() -> int:
            await asyncio.sleep(0)
            return threading.get_ident()

        ident = lt.run(answer, timeout=5)
        assert ident != threading.get_ident()
        assert lt.call(threading.get_ident, timeout=5) == ident
        assert lt.alive
    finally:
        lt.stop()
    assert not lt.alive
    lt.stop()  # twice is fine


def test_loop_thread_times_out():
    lt = LoopThread()
    try:

        async def slow() -> None:
            await asyncio.sleep(10)

        with pytest.raises(TimeoutError):
            lt.run(slow, timeout=0.05)
    finally:
        lt.stop()


def test_loop_thread_passes_errors_through():
    lt = LoopThread()
    try:

        def boom() -> None:
            raise ValueError("x")

        with pytest.raises(ValueError):
            lt.call(boom, timeout=5)
    finally:
        lt.stop()


def test_backoff_doubles_caps_and_jitters():
    b = Backoff(base=1, cap=60, jitter=0.2)
    assert [b.delay(n, lambda: 0.0) for n in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert b.delay(3, lambda: 1.0) == pytest.approx(6.4)
    assert b.delay(-1, lambda: 0.0) == 1


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_retry_until_succeeds_after_failures():
    clock = Clock()
    calls = iter([ConnectionError(), ConnectionError(), "ok"])
    seen: list[int] = []

    def fn():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    out = retry_until(
        fn,
        deadline_seconds=60,
        retry_on=(ConnectionError,),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        rand=lambda: 0.0,
        on_retry=lambda n, wait, exc: seen.append(n),
    )
    assert out == "ok"
    assert clock.sleeps == [1, 2]
    assert seen == [1, 2]


def test_retry_until_gives_up_at_the_deadline():
    clock = Clock()

    def fn():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry_until(
            fn,
            deadline_seconds=10,
            retry_on=(ConnectionError,),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            rand=lambda: 0.0,
        )
    assert clock.sleeps == [1, 2, 4]
    assert clock.t <= 10


def test_retry_until_does_not_retry_other_errors():
    with pytest.raises(ValueError):
        retry_until(lambda: (_ for _ in ()).throw(ValueError()), deadline_seconds=5,
                    retry_on=(ConnectionError,))  # fmt: skip


def test_token_bucket_paces():
    clock = Clock()
    bucket = TokenBucket(2, 2, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(4):
        bucket.acquire()
    assert clock.t == pytest.approx(1.0)
    with pytest.raises(ValueError):
        TokenBucket(0)
