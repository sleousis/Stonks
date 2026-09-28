"""The EODHD websocket source against a fake server on loopback (roadmap 21.1)."""

from __future__ import annotations

import socket
import threading
from itertools import islice

import pytest

from stonks.core.stream import Heartbeat, QuoteTick, TradeTick
from stonks.streaming.base import StreamAuthError, StreamConfigError, StreamDisconnectedError
from stonks.streaming.sources.eodhd_ws import EodhdStreamSource
from tests.fakes.eodhd_ws import FakeEodhdServer, load_messages

TRADES = load_messages("eodhd_us_trades")


def source(server: FakeEodhdServer, **kw) -> EodhdStreamSource:
    kw.setdefault("heartbeat_seconds", 0.2)
    return EodhdStreamSource(server.api_key, url=server.url, open_timeout=5, **kw)


def data_events(src: EodhdStreamSource, tickers: list[str], n: int) -> list:
    out = []
    for ev in src.stream(tickers):
        if not isinstance(ev, Heartbeat):
            out.append(ev)
        if len(out) >= n:
            src.close()
    return out


def test_streams_trades_with_lake_tickers():
    with FakeEodhdServer({"us": TRADES}) as server:
        src = source(server)
        got = data_events(src, ["AAPL.US", "MSFT.US"], len(TRADES))
    assert server.subscriptions == [("us", ["AAPL", "MSFT"])]
    assert all(isinstance(e, TradeTick) for e in got)
    assert [e.ticker for e in got[:2]] == ["AAPL.US", "MSFT.US"]
    assert [e.price for e in got] == [m["p"] for m in TRADES]


def test_only_subscribed_symbols_arrive():
    with FakeEodhdServer({"us": TRADES}) as server:
        src = source(server)
        n = sum(1 for m in TRADES if m["s"] == "AAPL")
        got = data_events(src, ["AAPL.US"], n)
    assert {e.ticker for e in got} == {"AAPL.US"}


def test_several_feeds_merge_into_one_stream():
    script = {
        "us": TRADES[:2],
        "us-quote": load_messages("eodhd_us_quotes"),
        "crypto": load_messages("eodhd_crypto"),
        "forex": load_messages("eodhd_forex"),
    }
    with FakeEodhdServer(script) as server:
        src = source(server, quotes=True)
        got = data_events(src, ["AAPL.US", "MSFT.US", "BTC-USD.CC", "EURUSD.FOREX"], 2 + 2 + 3 + 3)
    assert sorted(server.connects) == ["crypto", "forex", "us", "us-quote"]
    kinds = {(e.ticker, type(e).__name__) for e in got}
    assert ("BTC-USD.CC", "TradeTick") in kinds
    assert ("EURUSD.FOREX", "QuoteTick") in kinds
    assert ("AAPL.US", "QuoteTick") in kinds


def test_heartbeats_when_the_feed_is_quiet():
    with FakeEodhdServer({"us": []}) as server:
        src = source(server, heartbeat_seconds=0.05)
        beats = list(islice(src.stream(["AAPL.US"]), 3))
        src.close()
    assert all(isinstance(b, Heartbeat) for b in beats)


def test_a_wrong_key_is_an_auth_error_and_the_key_stays_secret():
    with FakeEodhdServer({"us": TRADES}, api_key="right") as server:
        src = EodhdStreamSource("wrong-key-123", url=server.url, open_timeout=5)
        with pytest.raises(StreamAuthError) as err:
            next(src.stream(["AAPL.US"]))
    assert "wrong-key-123" not in str(err.value)
    assert server.rejected == 1


def test_a_dropped_connection_raises_disconnected():
    with FakeEodhdServer({"us": TRADES}, drop_after_script=True) as server:
        src = source(server)
        seen = []
        with pytest.raises(StreamDisconnectedError, match="closed"):
            for ev in src.stream(["AAPL.US", "MSFT.US"]):
                seen.append(ev)  # noqa: PERF402  # keep what arrived before the drop
    assert len([e for e in seen if isinstance(e, TradeTick)]) == len(TRADES)


def test_an_unreachable_server_is_a_disconnect_without_the_key():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    src = EodhdStreamSource("secret-abc", url=f"ws://127.0.0.1:{port}/ws", open_timeout=2)
    with pytest.raises(StreamDisconnectedError) as err:
        next(src.stream(["AAPL.US"]))
    assert "secret-abc" not in str(err.value)


def test_no_streamable_ticker_is_a_config_error():
    src = EodhdStreamSource("k")
    with pytest.raises(StreamConfigError):
        next(src.stream(["VOD.LSE"]))


def test_close_from_another_thread_ends_the_stream():
    with FakeEodhdServer({"us": []}) as server:
        src = source(server, heartbeat_seconds=10)
        timer = threading.Timer(0.3, src.close)
        timer.start()
        assert [e for e in src.stream(["AAPL.US"]) if not isinstance(e, Heartbeat)] == []
        timer.join()


def test_quotes_carry_sizes():
    with FakeEodhdServer({"us-quote": load_messages("eodhd_us_quotes")}) as server:
        src = source(server, feeds={"US": "us-quote"})
        (q, _) = data_events(src, ["AAPL.US"], 2)
    assert isinstance(q, QuoteTick) and q.bid_size == 3 and q.ask_size == 2
