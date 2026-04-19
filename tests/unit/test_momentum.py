"""Unit tests for the Momentum reference strategy — a technical-indicator
rule-based strategy. Computes the N-day trailing return for each ticker;
emits buys for those above a configurable threshold and sells holdings whose
momentum has fallen below the threshold.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.momentum import Momentum


@pytest.fixture
def lake_with_trend(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()

    dates = pd.bdate_range(start="2026-01-02", periods=60)

    up = [100.0 * (1 + 0.003 * i) for i in range(len(dates))]
    flat = [50.0] * len(dates)
    down = [100.0 * (1 - 0.002 * i) for i in range(len(dates))]

    rows = []
    for ticker, closes in [("UP.US", up), ("FLAT.US", flat), ("DOWN.US", down)]:
        for d, c in zip(dates, closes, strict=False):
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c + 1,
                    "low": c - 1,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))
    yield lake
    lake.close()


def test_parameter_spec_exposes_lookback_and_threshold():
    names = {s.name for s in Momentum.parameter_spec()}
    assert {"lookback_days", "threshold"} <= names


def test_estimate_return_prefers_uptrending_over_downtrending(lake_with_trend):
    s = Momentum({"lookback_days": 20, "threshold": 0.0})
    as_of = date(2026, 3, 13)  # ~50 trading days in

    r_up = s.estimate_return("UP.US", as_of, lake_with_trend)
    r_down = s.estimate_return("DOWN.US", as_of, lake_with_trend)
    r_flat = s.estimate_return("FLAT.US", as_of, lake_with_trend)

    assert r_up is not None and r_up > 0
    assert r_down is None or r_down <= 0   # below threshold → filtered or negative
    assert r_flat is None or abs(r_flat) < 0.01


def test_estimate_return_returns_none_when_insufficient_history(lake_with_trend):
    s = Momentum({"lookback_days": 20, "threshold": 0.0})
    r = s.estimate_return("UP.US", date(2026, 1, 5), lake_with_trend)
    assert r is None


def test_extract_features_returns_lookback_return(lake_with_trend):
    s = Momentum({"lookback_days": 20, "threshold": 0.0})
    features = s.extract_features("UP.US", date(2026, 3, 13), lake_with_trend)
    assert "r_lookback" in features.values


def test_decide_buys_top_ranked_ticker(lake_with_trend):
    s = Momentum({"lookback_days": 20, "threshold": 0.0, "allocation": 1.0})
    portfolio = Portfolio(cash=10_000.0, positions={})
    prices = {"UP.US": 120.0, "FLAT.US": 50.0}
    orders = s.decide(
        my_picks=[(0.1, "UP.US"), (0.01, "FLAT.US")],
        portfolio=portfolio,
        prices=prices,
        as_of=date(2026, 3, 13),
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].ticker == "UP.US"


def test_decide_sells_positions_that_fell_off_ranking(lake_with_trend):
    s = Momentum({"lookback_days": 20, "threshold": 0.0, "allocation": 1.0})
    # already holding DOWN.US; it's no longer in my_picks → should sell
    portfolio = Portfolio(cash=0.0, positions={"DOWN.US": 20.0})
    prices = {"UP.US": 120.0, "DOWN.US": 80.0}
    orders = s.decide(
        my_picks=[(0.1, "UP.US")],
        portfolio=portfolio,
        prices=prices,
        as_of=date(2026, 3, 13),
    )
    sells = [o for o in orders if o.side == "sell"]
    assert any(o.ticker == "DOWN.US" for o in sells)
