"""The vendor-neutral stream events (roadmap 21.1)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from stonks.core.interval import Interval
from stonks.core.stream import (
    Heartbeat,
    QuoteTick,
    StreamBar,
    TradeTick,
    bucket_start,
    event_ticker,
)

T0 = datetime(2026, 9, 28, 13, 30, 15, 250_000, tzinfo=UTC)


def test_trade_timestamp_is_normalised_to_utc():
    east = timezone(timedelta(hours=-4))
    t = TradeTick("AAPL.US", datetime(2026, 9, 28, 9, 30, tzinfo=east), 200.0, 10)
    assert t.timestamp == datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
    assert t.kind == "trade"


def test_naive_timestamps_are_refused():
    with pytest.raises(ValueError, match="timezone"):
        TradeTick("AAPL.US", datetime(2026, 9, 28, 13, 30), 200.0, 1)


@pytest.mark.parametrize("price", [0.0, -1.0, math.nan, math.inf])
def test_trade_price_must_be_positive_and_finite(price):
    with pytest.raises(ValueError, match="price"):
        TradeTick("AAPL.US", T0, price, 1)


def test_trade_size_cannot_be_negative():
    with pytest.raises(ValueError, match="size"):
        TradeTick("AAPL.US", T0, 1.0, -5)


def test_ticker_is_required():
    with pytest.raises(ValueError, match="ticker"):
        TradeTick("", T0, 1.0, 1)


def test_quote_mid_and_reference():
    q = QuoteTick("AAPL.US", T0, bid=199.9, ask=200.1)
    assert q.mid == pytest.approx(200.0)
    assert q.reference == pytest.approx(200.0)
    assert QuoteTick("AAPL.US", T0, bid=199.9, ask=200.1, last=200.05).reference == 200.05
    assert QuoteTick("AAPL.US", T0, bid=None, ask=200.1).mid is None
    assert QuoteTick("AAPL.US", T0, bid=None, ask=None).reference is None


def test_crossed_or_bad_quote_has_no_mid():
    assert QuoteTick("X.US", T0, bid=0.0, ask=1.0).mid is None
    assert QuoteTick("X.US", T0, bid=2.0, ask=1.0).mid is None


def test_quote_prices_must_be_finite_when_set():
    with pytest.raises(ValueError, match="bid"):
        QuoteTick("X.US", T0, bid=math.nan, ask=1.0)


def test_bar_checks_its_range():
    bar = StreamBar("AAPL.US", T0, Interval.MIN_1, 1.0, 2.0, 0.5, 1.5, 100)
    assert bar.kind == "bar" and bar.adj_close == 1.5
    with pytest.raises(ValueError, match="high"):
        StreamBar("AAPL.US", T0, Interval.MIN_1, 1.0, 0.9, 0.5, 0.8, 1)
    with pytest.raises(ValueError, match="low"):
        StreamBar("AAPL.US", T0, Interval.MIN_1, 1.0, 2.0, 1.2, 1.5, 1)
    with pytest.raises(ValueError, match="volume"):
        StreamBar("AAPL.US", T0, Interval.MIN_1, 1.0, 2.0, 0.5, 1.5, -1)


def test_bar_interval_must_be_intraday():
    with pytest.raises(ValueError, match="intraday"):
        StreamBar("AAPL.US", T0, Interval.DAY_1, 1.0, 2.0, 0.5, 1.5, 1)


def test_heartbeat_has_no_ticker():
    hb = Heartbeat(T0, source="eodhd")
    assert hb.kind == "heartbeat"
    assert event_ticker(hb) is None
    assert event_ticker(TradeTick("A.US", T0, 1.0, 1)) == "A.US"


def test_bucket_start_floors_to_the_interval():
    assert bucket_start(T0, Interval.MIN_1) == datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
    assert bucket_start(T0, Interval.MIN_5) == datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
    late = datetime(2026, 9, 28, 13, 34, 59, 999_999, tzinfo=UTC)
    assert bucket_start(late, Interval.MIN_5) == datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
    assert bucket_start(late, Interval.HOUR_1) == datetime(2026, 9, 28, 13, 0, tzinfo=UTC)


def test_bucket_start_refuses_daily_intervals():
    with pytest.raises(ValueError, match="intraday"):
        bucket_start(T0, Interval.DAY_1)
