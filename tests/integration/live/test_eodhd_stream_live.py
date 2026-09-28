"""Live contract test of the EODHD websocket source (roadmap 21.1).

Gated behind ``STONKS_RUN_LIVE_TESTS=1`` and ``EODHD_API_KEY``. The crypto
feed trades around the clock, so it runs at any hour. The US trade feed only
has trades in market hours and skips outside them. A plan without websocket
access skips on the refused login, never fails.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from stonks.core.stream import Heartbeat, QuoteTick, TradeTick
from stonks.scheduling.calendar import get_calendar
from stonks.streaming.base import StreamAuthError
from stonks.streaming.sources.eodhd_ws import EodhdStreamSource

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
    pytest.mark.skipif(not os.environ.get("EODHD_API_KEY"), reason="EODHD_API_KEY not set"),
]

WAIT_SECONDS = 45.0


def first_events(src: EodhdStreamSource, tickers: list[str], want: int) -> list:
    got: list = []
    timer = threading.Timer(WAIT_SECONDS, src.close)
    timer.start()
    try:
        for ev in src.stream(tickers):
            if not isinstance(ev, Heartbeat):
                got.append(ev)
            if len(got) >= want:
                src.close()
    except StreamAuthError as exc:
        pytest.skip(f"the EODHD plan has no websocket access: {exc}")
    finally:
        timer.cancel()
    return got


def test_crypto_trades_arrive_with_lake_tickers():
    src = EodhdStreamSource(os.environ["EODHD_API_KEY"], heartbeat_seconds=2.0)
    got = first_events(src, ["BTC-USD.CC", "ETH-USD.CC"], 3)
    assert got, f"no crypto trade in {WAIT_SECONDS:g} s"
    assert all(isinstance(e, TradeTick) for e in got)
    assert {e.ticker for e in got} <= {"BTC-USD.CC", "ETH-USD.CC"}
    assert all(e.price > 0 and abs(e.timestamp.timestamp() - time.time()) < 3600 for e in got)


def test_forex_quotes_arrive():
    src = EodhdStreamSource(os.environ["EODHD_API_KEY"], heartbeat_seconds=2.0)
    got = first_events(src, ["EURUSD.FOREX"], 2)
    if not got:
        pytest.skip("no forex quote (the forex market may be closed)")
    assert all(isinstance(e, QuoteTick) and e.ticker == "EURUSD.FOREX" for e in got)


def test_us_trades_in_market_hours():
    from datetime import UTC, datetime

    if not get_calendar("XNYS").is_open_at(datetime.now(UTC)):
        pytest.skip("the US market is closed")
    src = EodhdStreamSource(os.environ["EODHD_API_KEY"], heartbeat_seconds=2.0)
    got = first_events(src, ["AAPL.US", "MSFT.US"], 3)
    assert got and all(e.ticker in ("AAPL.US", "MSFT.US") for e in got)
