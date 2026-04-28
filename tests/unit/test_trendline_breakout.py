"""Unit tests for TrendlineBreakoutStrategy — support/resistance breakout
with rolling linear fits. Complements DonchianBreakout (horizontal
channels) with slope-aware channels.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.trendline_breakout import TrendlineBreakoutStrategy


@pytest.fixture
def lake_trendline(tmp_path):
    """Lake with a flat base then an upward linear phase — clean
    trendline-breakout setup."""
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()

    dates = pd.bdate_range(start="2026-01-02", periods=120)
    closes = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 130.0, 60)])
    rows = [
        {
            "ticker": "X.US",
            "date": d.date(),
            "open": c,
            "high": c + 0.3,
            "low": c - 0.3,
            "close": c,
            "adj_close": c,
            "volume": 1_000_000,
        }
        for d, c in zip(dates, closes, strict=False)
    ]
    lake.upsert_prices(pd.DataFrame(rows))
    yield lake, dates
    lake.close()


def test_parameter_spec_lists_lookback_and_ticker():
    names = {s.name for s in TrendlineBreakoutStrategy.parameter_spec()}
    assert {"lookback", "interval", "ticker", "allocation"} <= names


def test_lookback_is_tunable_others_not():
    specs = {s.name: s for s in TrendlineBreakoutStrategy.parameter_spec()}
    assert specs["lookback"].tunable is True
    assert specs["ticker"].tunable is False
    assert specs["interval"].tunable is False
    assert specs["allocation"].tunable is False


def test_estimate_return_none_during_flat_phase(lake_trendline):
    lake, dates = lake_trendline
    strategy = TrendlineBreakoutStrategy({"lookback": 30, "ticker": "X.US"})
    r = strategy.estimate_return("X.US", dates[50].date(), lake)
    # during the flat phase the resistance is (near-)horizontal;
    # close is not beating it, so no long signal
    assert r is None


def test_estimate_return_positive_during_breakout_phase(lake_trendline):
    lake, dates = lake_trendline
    strategy = TrendlineBreakoutStrategy({"lookback": 30, "ticker": "X.US"})
    # deep into the rising phase a long signal should fire at least once
    hits = [strategy.estimate_return("X.US", dates[i].date(), lake) for i in range(90, 120)]
    assert any(h is not None and h > 0 for h in hits)


def test_estimate_return_none_for_wrong_ticker(lake_trendline):
    lake, dates = lake_trendline
    strategy = TrendlineBreakoutStrategy({"lookback": 30, "ticker": "X.US"})
    assert strategy.estimate_return("OTHER.US", dates[-1].date(), lake) is None


def test_decide_buys_on_long_signal_when_flat(lake_trendline):
    strategy = TrendlineBreakoutStrategy({"lookback": 30, "ticker": "X.US", "allocation": 1.0})
    portfolio = Portfolio(cash=10_000.0, positions={})
    orders = strategy.decide(
        my_picks=[(0.01, "X.US")],
        portfolio=portfolio,
        prices={"X.US": 130.0},
        as_of=date(2026, 3, 15),
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"


def test_decide_sells_on_no_signal_when_holding(lake_trendline):
    strategy = TrendlineBreakoutStrategy({"lookback": 30, "ticker": "X.US", "allocation": 1.0})
    portfolio = Portfolio(cash=0.0, positions={"X.US": 10.0})
    orders = strategy.decide(
        my_picks=[],
        portfolio=portfolio,
        prices={"X.US": 100.0},
        as_of=date(2026, 3, 15),
    )
    assert any(o.side == "sell" and o.ticker == "X.US" for o in orders)


def test_extract_features_includes_bands_and_signal(lake_trendline):
    lake, dates = lake_trendline
    strategy = TrendlineBreakoutStrategy({"lookback": 30, "ticker": "X.US"})
    features = strategy.extract_features("X.US", dates[100].date(), lake)
    assert "support" in features.values
    assert "resistance" in features.values
    assert "signal" in features.values


def test_extract_features_empty_when_insufficient_history(lake_trendline):
    lake, _ = lake_trendline
    strategy = TrendlineBreakoutStrategy({"lookback": 300, "ticker": "X.US"})
    features = strategy.extract_features("X.US", date(2026, 1, 3), lake)
    assert features.values == {}
