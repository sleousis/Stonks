"""Ticks to 1m bars (roadmap 21.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, QuoteTick, StreamBar, TradeTick
from stonks.streaming.bars import BarBuilder

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return M0 + timedelta(seconds=seconds)


def trade(ticker: str, seconds: float, price: float, size: float = 10) -> TradeTick:
    return TradeTick(ticker, at(seconds), price, size, source="test")


def test_trades_make_one_bar_per_minute():
    b = BarBuilder(grace=timedelta(0))
    assert b.on_event(trade("A.US", 1, 10.0)) == []
    assert b.on_event(trade("A.US", 20, 12.0, 5)) == []
    assert b.on_event(trade("A.US", 40, 9.0, 1)) == []
    assert b.on_event(trade("A.US", 59.9, 11.0, 4)) == []
    (bar,) = b.on_event(trade("A.US", 61, 11.5))
    assert bar == StreamBar("A.US", M0, Interval.MIN_1, 10.0, 12.0, 9.0, 11.0, 20, source="test")


def test_out_of_order_ticks_inside_a_minute_keep_time_order():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(trade("A.US", 30, 10.0))
    b.on_event(trade("A.US", 50, 11.0))
    b.on_event(trade("A.US", 5, 9.5))  # earlier trade arrives last
    b.on_event(trade("A.US", 40, 10.5))  # older than the last close
    (bar,) = b.close_due(at(60))
    assert bar.open == 9.5 and bar.close == 11.0
    assert bar.high == 11.0 and bar.low == 9.5


def test_bars_close_on_time_after_the_grace_period():
    b = BarBuilder(grace=timedelta(seconds=2))
    b.on_event(trade("A.US", 10, 10.0))
    assert b.close_due(at(61)) == []
    (bar,) = b.close_due(at(62))
    assert bar.timestamp == M0
    assert b.close_due(at(200)) == []


def test_heartbeats_close_due_bars():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(trade("A.US", 10, 10.0))
    b.on_event(trade("B.US", 10, 20.0))
    bars = b.on_event(Heartbeat(at(65)))
    assert sorted(x.ticker for x in bars) == ["A.US", "B.US"]


def test_late_ticks_for_a_closed_minute_are_dropped_and_counted():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(trade("A.US", 10, 10.0))
    b.on_event(trade("A.US", 70, 10.2))  # closes 13:30
    assert b.on_event(trade("A.US", 30, 99.0)) == []
    assert b.late_ticks == 1
    (bar,) = b.close_due(at(120))
    assert bar.timestamp == at(60) and bar.high == 10.2


def test_late_after_time_close_is_dropped_too():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(trade("A.US", 10, 10.0))
    b.close_due(at(60))
    assert b.on_event(trade("A.US", 50, 50.0)) == []
    assert b.late_ticks == 1
    assert b.close_due(at(600)) == []


def test_auto_mode_uses_quotes_only_for_tickers_without_trades():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(QuoteTick("EURUSD.FOREX", at(1), bid=1.10, ask=1.12))
    b.on_event(QuoteTick("EURUSD.FOREX", at(2), bid=1.12, ask=1.14))
    b.on_event(trade("A.US", 1, 10.0))
    b.on_event(QuoteTick("A.US", at(3), bid=50.0, ask=60.0))  # ignored: A.US trades
    bars = {x.ticker: x for x in b.close_due(at(60))}
    fx = bars["EURUSD.FOREX"]
    assert fx.open == pytest.approx(1.11) and fx.close == pytest.approx(1.13)
    assert fx.volume == 0
    assert bars["A.US"].high == 10.0


def test_quote_mode_prefers_the_last_price():
    b = BarBuilder(mode="quotes", grace=timedelta(0))
    b.on_event(trade("A.US", 1, 99.0))  # trades ignored in quote mode
    b.on_event(QuoteTick("A.US", at(2), bid=9.0, ask=11.0, last=10.5))
    b.on_event(QuoteTick("A.US", at(3), bid=None, ask=None))  # nothing usable
    (bar,) = b.close_due(at(60))
    assert bar.open == bar.close == 10.5


def test_trade_mode_ignores_quotes():
    b = BarBuilder(mode="trades", grace=timedelta(0))
    b.on_event(QuoteTick("EURUSD.FOREX", at(1), bid=1.10, ask=1.12))
    assert b.close_due(at(60)) == []


def test_source_bars_of_the_same_interval_pass_through_once():
    b = BarBuilder(grace=timedelta(0))
    bar = StreamBar("A.US", M0, Interval.MIN_1, 1.0, 2.0, 0.5, 1.5, 7, source="v")
    assert b.on_event(bar) == [bar]
    assert b.on_event(bar) == []  # the minute is closed now
    other = StreamBar("A.US", M0, Interval.MIN_5, 1.0, 2.0, 0.5, 1.5, 7)
    assert b.on_event(other) == []


def test_flush_closes_finished_minutes_but_keeps_the_open_one():
    b = BarBuilder(grace=timedelta(seconds=30))
    b.on_event(trade("A.US", 10, 10.0))
    b.on_event(trade("B.US", 65, 20.0))
    bars = b.flush(at(70))
    assert [x.ticker for x in bars] == ["A.US"]
    assert b.open_tickers() == ["B.US"]
    (rest,) = b.drain()
    assert rest.ticker == "B.US"
    assert b.open_tickers() == []


def test_fractional_sizes_round_into_volume():
    b = BarBuilder(grace=timedelta(0))
    b.on_event(trade("BTC-USD.CC", 1, 60_000.0, 0.4))
    b.on_event(trade("BTC-USD.CC", 2, 60_010.0, 0.7))
    (bar,) = b.close_due(at(60))
    assert bar.volume == 1


def test_five_minute_bars():
    b = BarBuilder(interval=Interval.MIN_5, grace=timedelta(0))
    b.on_event(trade("A.US", 10, 10.0))
    b.on_event(trade("A.US", 250, 11.0))
    assert b.close_due(at(299)) == []
    (bar,) = b.close_due(at(300))
    assert bar.interval == Interval.MIN_5 and bar.close == 11.0


def test_daily_interval_is_refused():
    with pytest.raises(ValueError, match="intraday"):
        BarBuilder(interval=Interval.DAY_1)
