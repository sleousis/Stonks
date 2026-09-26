"""``benchmark_curve`` over a real lake (BL-22): alignment, no look-ahead,
no survivorship, adjusted prices."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.benchmark import AUTO_BENCHMARK_TICKER, benchmark_curve
from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake

DAYS = [d.to_pydatetime() for d in pd.bdate_range("2024-06-03", periods=8)]


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def _bars(lake, ticker, timestamps, closes, *, adj=None, interval=Interval.DAY_1):
    closes = np.asarray(closes, dtype=float)
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": ticker,
                "timestamp": list(timestamps),
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes if adj is None else np.asarray(adj, dtype=float),
                "volume": 1000.0,
            }
        ),
        interval,
    )


def test_named_ticker_is_aligned_and_normalised(lake):
    _bars(lake, "B.US", DAYS, [10, 11, 12, 11, 13, 14, 15, 16])
    curve = benchmark_curve(lake, "B.US", DAYS, initial_value=100.0)
    assert curve is not None
    assert curve.name == "B.US"
    assert curve.dates == tuple(DAYS)
    assert curve.values == pytest.approx([100, 110, 120, 110, 130, 140, 150, 160])


def test_missing_bar_carries_the_last_close_forward(lake):
    _bars(lake, "B.US", [DAYS[0], DAYS[1], DAYS[3]], [10, 12, 15])
    curve = benchmark_curve(lake, "B.US", DAYS[:4])
    assert curve.values == pytest.approx([1.0, 1.2, 1.2, 1.5])


def test_no_look_ahead_uses_only_bars_at_or_before_each_date(lake):
    # hourly curve at :00, benchmark bars stamped 30 minutes later
    t0 = datetime(2024, 6, 3, 10)
    curve_dates = [t0 + timedelta(hours=i) for i in range(4)]
    bench_ts = [t + timedelta(minutes=30) for t in [t0 - timedelta(hours=1), *curve_dates]]
    _bars(lake, "B.US", bench_ts, [10, 20, 30, 40, 50], interval=Interval.HOUR_1)
    curve = benchmark_curve(lake, "B.US", curve_dates, interval=Interval.HOUR_1)
    # entry at the 09:30 price (the last one known at 10:00); the 10:30 bar
    # only shows up at 11:00
    assert curve.values == pytest.approx([1.0, 2.0, 3.0, 4.0])


def test_bars_after_the_window_do_not_change_the_curve(lake):
    _bars(lake, "B.US", DAYS, [10, 11, 12, 13, 14, 15, 16, 17])
    before = benchmark_curve(lake, "B.US", DAYS[:4]).values
    _bars(lake, "B.US", DAYS[4:], [1, 1, 1, 1], adj=[0.5, 0.5, 0.5, 0.5])
    assert benchmark_curve(lake, "B.US", DAYS[:4]).values == pytest.approx(before)


def test_split_is_adjusted_away(lake):
    _bars(lake, "B.US", DAYS[:4], [100, 100, 25, 25])
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": "B.US", "date": DAYS[2], "ratio": 4.0}]))
    assert benchmark_curve(lake, "B.US", DAYS[:4]).values == pytest.approx([1, 1, 1, 1])


def test_vendor_adj_close_counts_dividends(lake):
    # a 2% dividend on day 2: raw close drops, adj_close does not
    _bars(lake, "B.US", DAYS[:3], [100, 100, 98], adj=[98, 98, 98])
    assert benchmark_curve(lake, "B.US", DAYS[:3]).values == pytest.approx([1, 1, 1])


def test_ticker_listing_mid_window_holds_cash_until_its_first_bar(lake):
    _bars(lake, "B.US", DAYS[2:5], [10, 20, 30])
    assert benchmark_curve(lake, "B.US", DAYS[:5]).values == pytest.approx([1, 1, 1, 2, 3])


def test_unknown_ticker_and_none_give_no_curve(lake):
    assert benchmark_curve(lake, "NOPE.US", DAYS) is None
    assert benchmark_curve(lake, "none", DAYS, universe=["A.US"]) is None
    assert benchmark_curve(lake, None, DAYS, universe=["A.US"]) is None
    assert benchmark_curve(lake, "B.US", []) is None


def test_equal_weight_buys_the_universe_on_the_first_date(lake):
    _bars(lake, "A.US", DAYS[:4], [10, 20, 20, 30])
    _bars(lake, "B.US", DAYS[:4], [10, 10, 5, 10])
    curve = benchmark_curve(lake, "EW", DAYS[:4], universe=["A.US", "B.US"])
    assert curve.name == "EW"
    assert curve.members == ("A.US", "B.US")
    assert curve.values == pytest.approx([1.0, 1.5, 1.25, 2.0])


def test_equal_weight_excludes_tickers_entering_mid_window(lake):
    """Holding a name from day 0 that only lists later would need to know,
    on day 0, that it will list: survivorship/look-ahead. It is left out."""
    _bars(lake, "A.US", DAYS[:4], [10, 11, 12, 13])
    _bars(lake, "LATE.US", DAYS[2:4], [1, 100])
    curve = benchmark_curve(lake, "EW", DAYS[:4], universe=["A.US", "LATE.US", "NOBARS.US"])
    assert curve.members == ("A.US",)
    assert curve.excluded == ("LATE.US", "NOBARS.US")
    assert curve.values == pytest.approx([1.0, 1.1, 1.2, 1.3])


def test_equal_weight_skips_names_dead_before_the_window(lake):
    old = DAYS[0] - timedelta(days=30)
    _bars(lake, "A.US", DAYS[:3], [10, 11, 12])
    _bars(lake, "DEAD.US", [old], [5])
    curve = benchmark_curve(lake, "EW", DAYS[:3], universe=["A.US", "DEAD.US"])
    assert curve.members == ("A.US",)
    assert curve.values == pytest.approx([1.0, 1.1, 1.2])


def test_equal_weight_keeps_a_delisted_name_at_its_last_close(lake):
    _bars(lake, "A.US", DAYS[:4], [10, 10, 10, 10])
    _bars(lake, "D.US", DAYS[:2], [10, 5])
    curve = benchmark_curve(lake, "EW", DAYS[:4], universe=["A.US", "D.US"])
    assert curve.values == pytest.approx([1.0, 0.75, 0.75, 0.75])


def test_auto_prefers_the_market_ticker_when_it_covers_the_first_date(lake):
    _bars(lake, "A.US", DAYS[:3], [10, 20, 40])
    _bars(lake, AUTO_BENCHMARK_TICKER, DAYS[:3], [100, 101, 102])
    curve = benchmark_curve(lake, "auto", DAYS[:3], universe=["A.US"])
    assert curve.name == AUTO_BENCHMARK_TICKER
    assert curve.spec == "auto"


def test_auto_falls_back_to_equal_weight(lake):
    _bars(lake, "A.US", DAYS[:3], [10, 20, 40])
    curve = benchmark_curve(lake, "auto", DAYS[:3], universe=["A.US"])
    assert curve.name == "EW"
    assert curve.values == pytest.approx([1, 2, 4])


def test_auto_falls_back_when_the_market_ticker_starts_late(lake):
    _bars(lake, "A.US", DAYS[:3], [10, 20, 40])
    _bars(lake, AUTO_BENCHMARK_TICKER, DAYS[1:3], [100, 101])
    assert benchmark_curve(lake, "auto", DAYS[:3], universe=["A.US"]).name == "EW"


def test_date_objects_are_accepted(lake):
    _bars(lake, "B.US", DAYS[:2], [10, 12])
    curve = benchmark_curve(lake, "B.US", [d.date() for d in DAYS[:2]])
    assert curve.values == pytest.approx([1.0, 1.2])


# ---- review 18.1 edge cases ----------------------------------------------------------


def test_auto_on_a_crypto_universe_is_its_equal_weight(lake):
    """No SPY.US in a crypto lake: "auto" is the equal-weight crypto book,
    marked on every 24/7 timestamp."""
    hours = [datetime(2024, 6, 1) + timedelta(days=i) for i in range(7)]  # Sat..Fri
    _bars(lake, "BTC-USD.CC", hours, [100, 110, 120, 130, 140, 150, 160])
    _bars(lake, "ETH-USD.CC", hours, [10, 10, 10, 10, 10, 10, 10])
    curve = benchmark_curve(lake, "auto", hours, universe=["BTC-USD.CC", "ETH-USD.CC"])
    assert curve is not None and curve.name == "EW"
    assert curve.members == ("BTC-USD.CC", "ETH-USD.CC")
    assert curve.values[-1] == pytest.approx(0.5 * 1.6 + 0.5 * 1.0)


def test_a_named_ticker_with_only_stale_pre_window_bars_gives_no_curve(lake):
    old = [DAYS[0] - timedelta(days=30 + i) for i in range(3)][::-1]
    _bars(lake, "OLD.US", old, [10, 11, 12])
    assert benchmark_curve(lake, "OLD.US", DAYS) is None
