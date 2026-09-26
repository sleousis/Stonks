"""Unit tests for the Donchian breakout reference strategy."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.donchian_breakout import DonchianBreakout


@pytest.fixture
def lake_with_breakout(tmp_path):
    """Lake with a price series that is flat (100.0) for 30 bars, then
    jumps to 110 on bar 30 — a clean single breakout for a lookback<=30."""
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()

    dates = pd.bdate_range(start="2026-01-02", periods=60)
    closes = [100.0] * 30 + [110.0] * 30
    rows = [
        {
            "ticker": "X.US",
            "date": d.date(),
            "open": c,
            "high": c + 0.1,
            "low": c - 0.1,
            "close": c,
            "adj_close": c,
            "volume": 1_000_000,
        }
        for d, c in zip(dates, closes, strict=False)
    ]
    lake.upsert_prices(pd.DataFrame(rows))
    yield lake, dates
    lake.close()


def test_parameter_spec_has_expected_names():
    names = {s.name for s in DonchianBreakout.parameter_spec()}
    assert {"lookback", "ticker", "allocation", "interval"} <= names


def test_lookback_is_tunable_ticker_is_not():
    specs = {s.name: s for s in DonchianBreakout.parameter_spec()}
    assert specs["lookback"].tunable is True
    assert specs["ticker"].tunable is False
    assert specs["allocation"].tunable is False
    assert specs["interval"].tunable is False


def test_estimate_return_is_none_before_breakout(lake_with_breakout):
    lake, dates = lake_with_breakout
    strategy = DonchianBreakout({"lookback": 20, "ticker": "X.US"})
    # day 10: all flat, no breakout yet
    r = strategy.estimate_return("X.US", dates[10].date(), lake)
    assert r is None


def test_estimate_return_positive_after_breakout(lake_with_breakout):
    lake, dates = lake_with_breakout
    strategy = DonchianBreakout({"lookback": 20, "ticker": "X.US"})
    # day 31: first real breakout (close 110 > prev channel max 100)
    r = strategy.estimate_return("X.US", dates[31].date(), lake)
    assert r is not None
    assert r > 0


def test_estimate_return_none_for_non_target_ticker(lake_with_breakout):
    lake, dates = lake_with_breakout
    strategy = DonchianBreakout({"lookback": 20, "ticker": "X.US"})
    r = strategy.estimate_return("OTHER.US", dates[31].date(), lake)
    assert r is None


def test_decide_buys_on_breakout_when_flat(lake_with_breakout):
    strategy = DonchianBreakout({"lookback": 20, "ticker": "X.US", "allocation": 1.0})
    portfolio = Portfolio(cash=10_000.0, positions={})
    orders = strategy.decide(
        my_picks=[(0.1, "X.US")],
        portfolio=portfolio,
        prices={"X.US": 110.0},
        as_of=date(2026, 2, 15),
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].ticker == "X.US"


def test_decide_sells_on_breakdown_when_holding(lake_with_breakout):
    strategy = DonchianBreakout({"lookback": 20, "ticker": "X.US", "allocation": 1.0})
    portfolio = Portfolio(cash=0.0, positions={"X.US": 50.0})
    orders = strategy.decide(
        my_picks=[],  # no pick → signal has gone off
        portfolio=portfolio,
        prices={"X.US": 90.0},
        as_of=date(2026, 2, 15),
    )
    sells = [o for o in orders if o.side == "sell"]
    assert any(o.ticker == "X.US" for o in sells)


def test_extract_features_returns_bands_when_computable(lake_with_breakout):
    lake, dates = lake_with_breakout
    strategy = DonchianBreakout({"lookback": 10, "ticker": "X.US"})
    features = strategy.extract_features("X.US", dates[35].date(), lake)
    assert "upper_band" in features.values
    assert "lower_band" in features.values
    assert "signal" in features.values


def test_extract_features_empty_when_lake_short(lake_with_breakout):
    lake, _ = lake_with_breakout
    # fixture has 60 bars; asking for 252 can't be satisfied
    strategy = DonchianBreakout({"lookback": 252, "ticker": "X.US"})
    out = strategy.extract_features("X.US", date(2026, 1, 3), lake)
    assert out.values == {}


def test_long_state_survives_a_long_range_after_the_breakout(tmp_path):
    """RS-31: the state must not depend on how many bars are replayed."""
    lake = DuckDBLake(tmp_path / "range.duckdb")
    lake.migrate()
    dates = pd.bdate_range(start="2025-01-02", periods=260)
    closes = [100.0] * 30 + [120.0] * 230  # one breakout, then 229 bars of range
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "X.US",
                "date": [d.date() for d in dates],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000_000.0,
            }
        )
    )
    try:
        strategy = DonchianBreakout({"lookback": 20, "ticker": "X.US"})
        for i in (40, 120, 259):
            assert strategy.estimate_return("X.US", dates[i].date(), lake) is not None, i
    finally:
        lake.close()
