"""The event driver: one loop for replay, backtest and live (roadmap 21.2.1)."""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest

from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, StreamBar, StreamEvent, TradeTick
from stonks.engine.driver import BarClose, EventDriver
from stonks.streaming.base import StreamContext, StreamDisconnectedError, StreamingSource
from stonks.streaming.sources.lake_bars import LakeBarSource
from stonks.streaming.writer import BarWriter

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ONE = timedelta(minutes=1)
SEC = timedelta(seconds=1)


def bar(minute: int, ticker: str = "A.US", close: float = 10.0) -> StreamBar:
    return StreamBar(
        ticker, M0 + minute * ONE, Interval.MIN_1, close, close + 1, close - 1, close, 3
    )


class ListSource(StreamingSource):
    """A finite source over a list of events."""

    finite = True

    def __init__(self, events: Sequence[StreamEvent], *, finite: bool = True) -> None:
        self.events = list(events)
        self.closed = False
        self._finite = finite

    @classmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        raise NotImplementedError

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        for event in self.events:
            if self.closed:
                return
            yield event

    def close(self) -> None:
        self.closed = True


class LiveListSource(ListSource):
    finite = False


class Recorder:
    """A handler that records what it saw and when."""

    def __init__(self, clock: FakeClock | None = None, name: str = "recorder") -> None:
        self.name = name
        self.clock = clock
        self.seen: list[BarClose] = []
        self.times: list[datetime] = []

    def on_bar_close(self, event: BarClose) -> None:
        self.seen.append(event)
        if self.clock is not None:
            self.times.append(self.clock.now())


def run(events: Sequence[StreamEvent], *handlers, clock: FakeClock | None = None) -> EventDriver:
    driver = EventDriver(ListSource(events), clock=clock or FakeClock(M0 - ONE))
    for h in handlers:
        driver.register(h)
    driver.run(["A.US", "B.US"])
    return driver


def test_bars_that_close_together_dispatch_as_one_event_sorted_by_ticker():
    rec = Recorder()
    run([bar(0, "B.US"), bar(0, "A.US"), bar(1, "A.US"), bar(1, "B.US")], rec)
    assert [(e.sequence, e.at, e.tickers) for e in rec.seen] == [
        (1, M0 + ONE, ("A.US", "B.US")),
        (2, M0 + 2 * ONE, ("A.US", "B.US")),
    ]
    assert rec.seen[0].bar("B.US") == bar(0, "B.US")
    assert rec.seen[0].bar("C.US") is None
    assert rec.seen[0].interval == Interval.MIN_1


def test_the_fake_clock_reads_the_decision_time_at_dispatch():
    clock = FakeClock(M0 - ONE)
    rec = Recorder(clock)
    driver = run([bar(0), bar(1), bar(2)], rec, clock=clock)
    settle = driver.settle
    assert rec.times == [M0 + m * ONE + settle for m in (1, 2, 3)]
    # and no bar in an event closed after the clock
    for event, now in zip(rec.seen, rec.times, strict=True):
        assert all(b.timestamp + ONE <= now for b in event.bars)


def test_ticks_become_bars_through_the_builder():
    rec = Recorder()
    events = [
        TradeTick("A.US", M0 + 5 * SEC, 10.0, 1),
        TradeTick("B.US", M0 + 20 * SEC, 20.0, 1),
        TradeTick("A.US", M0 + 50 * SEC, 11.0, 1),
        Heartbeat(M0 + ONE + 5 * SEC),
        TradeTick("A.US", M0 + ONE + 10 * SEC, 12.0, 1),
    ]
    driver = run(events, rec)
    assert [e.tickers for e in rec.seen] == [("A.US", "B.US"), ("A.US",)]
    first = rec.seen[0].bar("A.US")
    assert first is not None and (first.open, first.close) == (10.0, 11.0)
    assert driver.stats.bars == 3


def test_same_input_gives_the_same_event_sequence():
    events = [bar(m, t, 10.0 + m) for m in range(20) for t in ("A.US", "B.US", "C.US")]

    def trace(evs):
        clock = FakeClock(M0 - ONE)
        rec = Recorder(clock)
        run(evs, rec, clock=clock)
        return [(e.sequence, e.at, e.bars) for e in rec.seen], rec.times

    first = trace(events)
    assert trace(events) == first
    # arrival order inside one minute does not matter either
    shuffled: list[StreamEvent] = []
    for m in range(20):
        group = events[m * 3 : m * 3 + 3]
        random.Random(m).shuffle(group)
        shuffled.extend(group)
    assert trace(shuffled) == first


def test_handlers_run_in_priority_then_registration_order():
    calls: list[str] = []
    driver = EventDriver(ListSource([bar(0)]), clock=FakeClock(M0))
    driver.register(lambda e: calls.append("late"), name="late", priority=10)
    driver.register(lambda e: calls.append("first"), name="first")
    driver.register(lambda e: calls.append("second"), name="second")
    driver.register(lambda e: calls.append("gate"), name="gate", priority=-10)
    assert driver.handler_names() == ["gate", "first", "second", "late"]
    driver.run(["A.US"])
    assert calls == ["gate", "first", "second", "late"]


def test_handler_names_are_unique():
    driver = EventDriver(ListSource([]), clock=FakeClock(M0))
    driver.register(Recorder(name="x"))
    with pytest.raises(ValueError, match="already"):
        driver.register(Recorder(name="x"))


def test_a_handler_that_raises_is_isolated_and_counted():
    rec = Recorder()

    def boom(event: BarClose) -> None:
        raise RuntimeError("bad handler")

    driver = EventDriver(ListSource([bar(0), bar(1), bar(2)]), clock=FakeClock(M0))
    driver.register(boom, name="boom", priority=-1)
    driver.register(rec)
    stats = driver.run(["A.US"])
    assert len(rec.seen) == 3
    assert stats.handler_errors == {"boom": 3}
    assert stats.total_handler_errors == 3
    assert stats.bar_closes == 3


def test_a_late_bar_is_dropped_and_counted():
    rec = Recorder()
    driver = run([bar(0), bar(1), bar(2), bar(0, "B.US")], rec)
    assert [e.at for e in rec.seen] == [M0 + m * ONE for m in (1, 2, 3)]
    assert all("B.US" not in e.tickers for e in rec.seen)
    assert driver.stats.late_bars == 1


def test_a_bar_of_another_interval_is_ignored():
    rec = Recorder()
    five = StreamBar("A.US", M0, Interval.MIN_5, 10, 11, 9, 10, 1)
    run([five, bar(0)], rec)
    assert [e.tickers for e in rec.seen] == [("A.US",)]


def test_live_mode_leaves_the_clock_alone_and_dispatches_after_settle():
    class Wall:
        def __init__(self) -> None:
            self.t = M0 + ONE

        def now(self) -> datetime:
            return self.t

    wall = Wall()
    rec = Recorder()
    driver = EventDriver(LiveListSource([]), clock=wall)
    assert not driver.replay
    driver.register(rec)
    assert driver.on_event(bar(0)) == []  # closed at M0+1m, not settled yet
    wall.t = M0 + ONE + driver.settle
    (event,) = driver.on_event(Heartbeat(wall.t))
    assert event.tickers == ("A.US",)
    assert wall.t == M0 + ONE + driver.settle


def test_a_live_stream_that_ends_keeps_the_bar_in_progress():
    rec = Recorder()
    source = LiveListSource(
        [TradeTick("A.US", M0 + 5 * SEC, 10.0, 1), TradeTick("A.US", M0 + ONE, 11.0, 1)]
    )
    driver = EventDriver(source, clock=FakeClock(M0))
    driver.register(rec)
    driver.run(["A.US"])
    # the first minute is dispatched, the minute in progress is not
    assert [e.at for e in rec.seen] == [M0 + ONE]


def test_a_disconnect_propagates_and_the_next_run_carries_on():
    class Dropping(ListSource):
        def stream(self, tickers):
            yield bar(0)
            yield bar(1)
            raise StreamDisconnectedError("gone")

    rec = Recorder()
    driver = EventDriver(Dropping([]), clock=FakeClock(M0))
    driver.register(rec)
    with pytest.raises(StreamDisconnectedError):
        driver.run(["A.US"])
    # the minute that closed last waits: another ticker's bar may still come
    assert [e.at for e in rec.seen] == [M0 + ONE]
    assert driver.pending_closes == 1
    driver.source = ListSource([bar(1, "B.US"), bar(2)])
    driver.run(["A.US", "B.US"])
    assert [(e.at, e.tickers) for e in rec.seen] == [
        (M0 + ONE, ("A.US",)),
        (M0 + 2 * ONE, ("A.US", "B.US")),
        (M0 + 3 * ONE, ("A.US",)),
    ]


def test_stop_ends_the_run():
    rec = Recorder()
    driver = EventDriver(ListSource([bar(m) for m in range(10)]), clock=FakeClock(M0))

    def stopper(event: BarClose) -> None:
        if event.sequence == 2:
            driver.stop()

    driver.register(stopper)
    driver.register(rec)
    driver.run(["A.US"])
    assert len(rec.seen) < 10
    assert driver.source.closed  # type: ignore[attr-defined]


def test_the_clock_never_moves_backwards():
    clock = FakeClock(M0)
    times: list[datetime] = []
    events = [bar(0), TradeTick("B.US", M0 + 30 * SEC, 5.0, 1), bar(1), Heartbeat(M0)]
    driver = EventDriver(ListSource(events), clock=clock)
    driver.register(lambda e: times.append(clock.now()), name="t")
    driver.run(["A.US", "B.US"])
    assert times == sorted(times)
    assert clock.now() >= times[-1]


def test_backtest_and_replay_share_the_loop(lake):
    # lake bars through the lake source give the same events as the same
    # bars handed to the driver directly
    bars = [bar(m, t, 10.0 + m) for m in range(5) for t in ("A.US", "B.US")]
    w = BarWriter(lake.bar_store)
    w.add(bars)
    w.flush()

    clock = FakeClock(M0 - ONE)
    lake_rec = Recorder(clock)
    driver = EventDriver(LakeBarSource(lake, start=M0, end=M0 + 5 * ONE, clock=clock), clock=clock)
    driver.register(lake_rec)
    driver.run(["A.US", "B.US"])

    list_clock = FakeClock(M0 - ONE)
    list_rec = Recorder(list_clock)
    run(bars, list_rec, clock=list_clock)

    def shape(rec: Recorder):
        return [
            (e.at, [(b.ticker, b.timestamp, b.open, b.close, b.volume) for b in e.bars])
            for e in rec.seen
        ]

    assert len(lake_rec.seen) == 5
    assert shape(lake_rec) == shape(list_rec)
    assert lake_rec.times == list_rec.times
