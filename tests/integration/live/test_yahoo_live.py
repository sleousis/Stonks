"""Live contract test against real Yahoo Finance via yfinance. Gated behind
STONKS_RUN_LIVE_TESTS=1 so the default pytest run is hermetic."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.ingest.sources.yahoo import YahooDataSource

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
]


@pytest.fixture
def source():
    return YahooDataSource()


def test_live_daily_prices(source):
    since = datetime.now(UTC).date() - timedelta(days=14)
    bars = list(source.fetch_prices("AAPL.US", since=since))
    assert bars
    assert all(b.ticker == "AAPL.US" and b.date >= since for b in bars)
    assert all(b.low <= b.close <= b.high for b in bars)


def test_live_intraday_bars_are_naive_utc(source):
    bars = list(
        source.fetch_intraday_bars(
            "BMW.XETRA", Interval.HOUR_1, since=datetime.now(UTC).date() - timedelta(days=7)
        )
    )
    assert bars
    assert all(b.timestamp.tzinfo is None for b in bars)


def test_live_profile(source):
    profile = source.fetch_metadata("AAPL.US").profile
    assert profile.currency == "USD"
    assert profile.sector
