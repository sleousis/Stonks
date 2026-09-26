"""Bar-count lookback windows for strategies.

Intraday markets close overnight and at weekends, so a window sized in
wall-clock time (``interval * N``) can hold far fewer than ``N`` bars. These
tests pin that strategies fetch enough history to see ``N`` bars regardless
of session gaps.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.strategies._common import get_last_n_bars
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.rsi_pca import RSIPCAStrategy
from stonks.strategies.examples.trendline_breakout import TrendlineBreakoutStrategy

# Monday, with only the 14:00 and 15:00 session bars printed so far.
MONDAY_AS_OF = datetime(2026, 2, 2, 15, 0)


def _session_timestamps(days: pd.DatetimeIndex) -> list[datetime]:
    """Seven hourly bars per weekday (14:00..20:00), like a US cash session."""
    return [datetime(d.year, d.month, d.day, h) for d in days for h in range(14, 21)]


def _bars_frame(ticker: str, timestamps: list[datetime], closes: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ticker,
            "timestamp": timestamps,
            "open": closes,
            "high": closes + 0.2,
            "low": closes - 0.2,
            "close": closes,
            "adj_close": closes,
            "volume": 1_000,
        }
    )


@pytest.fixture
def hourly_lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    timestamps = _session_timestamps(pd.bdate_range("2026-01-05", "2026-02-02"))
    rng = np.random.default_rng(3)
    closes = 100.0 + np.cumsum(rng.normal(0.0, 0.5, len(timestamps)))
    lake.upsert_bars(_bars_frame("X.US", timestamps, closes), Interval.HOUR_1)
    yield lake
    lake.close()


# ---- helper ---------------------------------------------------------------


def test_get_last_n_bars_spans_a_weekend(hourly_lake):
    df = get_last_n_bars(hourly_lake, "X.US", Interval.HOUR_1, MONDAY_AS_OF, 20)
    assert len(df) == 20
    assert df["timestamp"].iloc[-1] == pd.Timestamp(MONDAY_AS_OF)
    assert df["timestamp"].is_monotonic_increasing
    assert list(df.index) == list(range(20))


def test_get_last_n_bars_spans_a_long_holiday_gap(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    # two short stretches of sessions separated by a three-week gap
    early = _session_timestamps(pd.bdate_range("2026-01-05", "2026-01-09"))
    late = _session_timestamps(pd.bdate_range("2026-02-02", "2026-02-02"))
    timestamps = early + late
    lake.upsert_bars(
        _bars_frame("X.US", timestamps, np.linspace(100, 110, len(timestamps))),
        Interval.HOUR_1,
    )
    try:
        df = get_last_n_bars(lake, "X.US", Interval.HOUR_1, datetime(2026, 2, 2, 20), 30)
        assert len(df) == 30
    finally:
        lake.close()


def test_get_last_n_bars_returns_what_exists_when_history_is_short(hourly_lake):
    df = get_last_n_bars(hourly_lake, "X.US", Interval.HOUR_1, MONDAY_AS_OF, 10_000)
    assert 0 < len(df) < 10_000


def test_get_last_n_bars_daily_counts_trading_days(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    days = pd.bdate_range("2025-01-02", periods=300)
    closes = np.linspace(50, 80, len(days))
    lake.upsert_bars(_bars_frame("X.US", [d.to_pydatetime() for d in days], closes), Interval.DAY_1)
    try:
        df = get_last_n_bars(lake, "X.US", Interval.DAY_1, days[-1].date(), 252)
        assert len(df) == 252
        assert df["close"].iloc[-1] == pytest.approx(80.0)
    finally:
        lake.close()


# ---- strategies on a Monday ------------------------------------------------


def test_donchian_hourly_has_signal_on_monday(hourly_lake):
    s = DonchianBreakout({"lookback": 20, "interval": "1h", "ticker": "X.US"})
    features = s.extract_features("X.US", MONDAY_AS_OF, hourly_lake)
    assert "signal" in features.values


def test_trendline_hourly_has_signal_on_monday(hourly_lake):
    s = TrendlineBreakoutStrategy({"lookback": 20, "interval": "1h", "ticker": "X.US"})
    features = s.extract_features("X.US", MONDAY_AS_OF, hourly_lake)
    assert "signal" in features.values


def test_rsi_pca_hourly_has_prediction_on_monday(hourly_lake):
    s = RSIPCAStrategy({"interval": "1h", "ticker": "X.US", "n_components": 2})
    n_rsi = int(s.params["rsi_period_max"]) - int(s.params["rsi_period_min"])
    # hand-set a fitted state; the test is about the history window, not the fit
    s._rsi_means = np.full(n_rsi, 50.0)
    s._evecs = np.eye(n_rsi)[:, :2]
    s._coefs = np.array([0.001, 0.0])
    s._long_thresh = 1.0
    s._short_thresh = -1.0
    features = s.extract_features("X.US", MONDAY_AS_OF, hourly_lake)
    assert "pred" in features.values
