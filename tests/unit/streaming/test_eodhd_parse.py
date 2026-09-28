"""EODHD websocket messages to stream events (roadmap 21.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.config import Settings
from stonks.core.stream import QuoteTick, TradeTick
from stonks.streaming.base import StreamConfigError, StreamContext
from stonks.streaming.registry import build_stream_source, stream_source_classes
from stonks.streaming.sources.eodhd_ws import EodhdStreamSource, feed_kind, parse_message
from tests.fakes.eodhd_ws import load_messages

T0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


def test_feed_kinds():
    assert feed_kind("us") == "trades"
    assert feed_kind("us-quote") == "quotes"
    assert feed_kind("crypto") == "crypto"
    assert feed_kind("forex") == "forex"


def test_us_trade():
    msg = load_messages("eodhd_us_trades")[0]
    ev = parse_message("trades", msg, "AAPL.US")
    assert ev == TradeTick(
        "AAPL.US", datetime(2026, 9, 28, 13, 30, 1, 150_000, tzinfo=UTC), 227.31, 100, "eodhd"
    )


def test_us_quote():
    ev = parse_message("quotes", load_messages("eodhd_us_quotes")[0], "AAPL.US")
    assert isinstance(ev, QuoteTick)
    assert (ev.bid, ev.ask, ev.bid_size, ev.ask_size) == (227.30, 227.33, 3, 2)
    assert ev.timestamp == datetime(2026, 9, 28, 13, 30, 1, tzinfo=UTC)


def test_crypto_trade_prices_are_strings():
    ev = parse_message("crypto", load_messages("eodhd_crypto")[0], "BTC-USD.CC")
    assert ev == TradeTick(
        "BTC-USD.CC", datetime(2026, 9, 28, 13, 30, 0, 500_000, tzinfo=UTC), 60012.5, 0.015, "eodhd"
    )


def test_forex_quote():
    ev = parse_message("forex", load_messages("eodhd_forex")[0], "EURUSD.FOREX")
    assert isinstance(ev, QuoteTick)
    assert (ev.bid, ev.ask) == (1.10508, 1.10512)


@pytest.mark.parametrize(
    "msg",
    [
        {"s": "AAPL", "p": 0, "v": 1, "t": 1},
        {"s": "AAPL", "v": 1, "t": 1},
        {"s": "AAPL", "p": 1.0, "v": 1},
        {"s": "AAPL", "p": "abc", "v": 1, "t": 1},
    ],
)
def test_bad_trades_are_skipped(msg):
    assert parse_message("trades", msg, "AAPL.US") is None


def test_a_quote_without_either_side_is_skipped():
    assert parse_message("quotes", {"s": "AAPL", "t": 1}, "AAPL.US") is None


def make(**kw) -> EodhdStreamSource:
    return EodhdStreamSource("k", **kw)


def test_plan_groups_tickers_by_feed():
    src = make(quotes=True)
    plan = src.plan(["AAPL.US", "MSFT.US", "BTC-USD.CC", "EURUSD.FOREX", "VOD.LSE"])
    assert plan == {
        "us": {"AAPL": "AAPL.US", "MSFT": "MSFT.US"},
        "us-quote": {"AAPL": "AAPL.US", "MSFT": "MSFT.US"},
        "crypto": {"BTC-USD": "BTC-USD.CC"},
        "forex": {"EURUSD": "EURUSD.FOREX"},
    }
    assert src.supports("AAPL.US") and not src.supports("VOD.LSE")
    assert not src.supports("NOSUFFIX")


def test_is_registered_and_needs_a_key(monkeypatch):
    assert stream_source_classes()["eodhd"] is EodhdStreamSource
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    settings = Settings()
    settings.sources.eodhd.api_key = None
    with pytest.raises(StreamConfigError, match="EODHD_API_KEY"):
        build_stream_source(StreamContext(settings=settings), "eodhd")


def test_builds_from_settings():
    settings = Settings()
    settings.sources.eodhd.api_key = "secret-key"
    settings.streaming.eodhd.quotes = True
    settings.streaming.heartbeat_seconds = 1.5
    src = build_stream_source(StreamContext(settings=settings), "eodhd")
    assert isinstance(src, EodhdStreamSource)
    assert src.quotes and src.heartbeat_seconds == 1.5
    assert "secret-key" not in repr(src)
