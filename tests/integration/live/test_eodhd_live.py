"""Live contract test against real EODHD. Gated behind STONKS_RUN_LIVE_TESTS=1
and the presence of EODHD_API_KEY so the default pytest run is hermetic.

Uses AAPL.US (free-tier eligible) and a recent window (free tier caps history
to ~1 year). Fundamentals are paid-only; the test tolerates either success OR
the canonical EodhdFreeTierError so this file works on both subscription tiers.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from stonks.ingest.schemas import RawPriceBar
from stonks.ingest.sources.eodhd import EodhdDataSource, EodhdFreeTierError

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("EODHD_API_KEY"),
        reason="EODHD_API_KEY not set",
    ),
]


@pytest.fixture
def source():
    return EodhdDataSource(api_key=os.environ["EODHD_API_KEY"])


def test_live_fetch_prices_aapl(source):
    today = date.today()
    # stay well inside the free-tier one-year window
    since = today - timedelta(days=14)
    until = today - timedelta(days=1)

    bars = list(source.fetch_prices("AAPL.US", since=since, until=until))

    assert bars, "expected at least one price bar within the last 14 days"
    assert all(isinstance(b, RawPriceBar) for b in bars)
    assert all(b.ticker == "AAPL.US" for b in bars)
    assert all(since <= b.date <= until for b in bars)


def test_live_fundamentals_either_succeeds_or_reports_free_tier(source):
    try:
        rows = list(source.fetch_fundamentals("AAPL.US"))
    except EodhdFreeTierError:
        pytest.skip("fundamentals endpoint is paid-only; free tier returned the expected error")
    else:
        assert rows, "paid tier should yield at least one fundamental row"
