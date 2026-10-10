"""Delayed quotes never pass as live bars (IBKR paper accounts)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.core.stream import QuoteTick, StreamBar, TradeTick
from stonks.streaming.bars import BarBuilder
from stonks.streaming.health import StreamHealth, stream_metric_families
from stonks.streaming.settings import StreamingSettings
from stonks.streaming.writer import BarWriter

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return M0 + timedelta(seconds=seconds)


def quote(seconds: float, last: float, *, delayed: bool, ticker: str = "A.US") -> QuoteTick:
    return QuoteTick(ticker, at(seconds), bid=None, ask=None, last=last, delayed=delayed)


def test_the_setting_is_off_by_default():
    assert StreamingSettings().accept_delayed_quotes is False
    assert StreamingSettings().delayed_lag_seconds == 900


def test_delayed_quotes_never_build_bars_by_default():
    b = BarBuilder(grace=timedelta(0))
    assert b.on_event(quote(1, 10.0, delayed=True)) == []
    assert b.on_event(quote(61, 11.0, delayed=True)) == []
    assert b.drain() == []
    assert b.delayed_dropped == 2
    assert b.ticks_used == 0


def test_live_quotes_still_build_bars_when_delayed_ones_are_dropped():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(quote(1, 10.0, delayed=False))
    b.on_event(quote(2, 99.0, delayed=True))
    (bar,) = b.drain()
    assert bar.close == 10.0 and bar.delayed is False


def test_accepted_delayed_quotes_build_bars_marked_delayed():
    b = BarBuilder(grace=timedelta(0), accept_delayed=True)
    b.on_event(quote(1, 10.0, delayed=True))
    (bar,) = b.on_event(quote(61, 11.0, delayed=True))
    assert bar.delayed is True and bar.close == 10.0
    assert b.delayed_dropped == 0


def test_one_delayed_tick_marks_the_whole_bar_delayed():
    b = BarBuilder(grace=timedelta(0), accept_delayed=True)
    b.on_event(quote(1, 10.0, delayed=False))
    b.on_event(quote(30, 10.5, delayed=True))
    (bar,) = b.drain()
    assert bar.delayed is True


def test_a_delayed_source_bar_is_dropped_unless_accepted():
    bar = StreamBar("A.US", M0, Interval.MIN_1, 10, 10, 10, 10, delayed=True)
    assert BarBuilder().on_event(bar) == []
    assert BarBuilder(accept_delayed=True).on_event(bar) == [bar]


def test_trades_are_never_delayed():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(TradeTick("A.US", at(1), 10.0, 1))
    (bar,) = b.drain()
    assert bar.delayed is False


class _Store:
    def __init__(self) -> None:
        self.frames: list = []

    def upsert(self, frame) -> None:
        self.frames.append(frame)


def test_the_writer_keeps_delayed_bars_out_of_the_lake():
    store = _Store()
    writer = BarWriter(store)  # type: ignore[arg-type]
    live = StreamBar("A.US", M0, Interval.MIN_1, 10, 10, 10, 10)
    late = StreamBar("B.US", M0, Interval.MIN_1, 10, 10, 10, 10, delayed=True)
    writer.add([live, late])
    assert writer.flush() == 1
    assert writer.delayed_skipped == 1
    assert list(store.frames[0]["ticker"]) == ["A.US"]


def test_health_reports_a_source_that_sends_only_delayed_data():
    h = StreamHealth(source="ibkr")
    assert h.delayed_only is False
    h.count(quote(1, 10.0, delayed=True), at(1))
    h.count(quote(2, 10.0, delayed=True), at(2))
    assert h.delayed_only is True
    snap = h.snapshot()
    assert snap["delayed_events"] == 2 and snap["delayed_only"] is True
    back = StreamHealth.from_snapshot(snap)
    assert back.delayed_only is True and back.delayed_events == 2
    fams = {f.name: f for f in stream_metric_families(h)}
    assert fams["stonks_stream_delayed_only"].samples[0].value == 1.0
    assert fams["stonks_stream_delayed_events_total"].samples[0].value == 2.0


@pytest.mark.parametrize("live", [TradeTick("A.US", M0, 10.0, 1), QuoteTick("A.US", M0, 9.9, 10.1)])
def test_any_live_data_clears_delayed_only(live):
    h = StreamHealth(source="ibkr")
    h.count(quote(1, 10.0, delayed=True), at(1))
    h.count(live, at(2))
    assert h.delayed_only is False
