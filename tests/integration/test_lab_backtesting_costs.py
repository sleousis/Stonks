"""Lab backtests honor the dataset's cost model ([backtest.costs])."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import FinalReturnObjective
from stonks.strategies.examples.buy_and_hold import BuyAndHold


def _ds(lake, costs=None):
    return LabDataset(
        lake=lake,
        universe=["UP.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=1.0,
        costs=costs,
    )


def test_no_costs_by_default(lake_trending):
    strategy = BuyAndHold({"ticker": "UP.US"})
    ds = _ds(lake_trending)
    zero = _ds(lake_trending, CostModelSettings())
    a = run_backtest(strategy, ds, ds.full_window)
    b = run_backtest(BuyAndHold({"ticker": "UP.US"}), zero, zero.full_window)
    assert a.equity_curve == b.equity_curve


def test_configured_costs_reduce_returns(lake_trending):
    costly = CostModelSettings(default=AssetClassCosts(fee_flat=100.0, half_spread_bps=50.0))
    free = _ds(lake_trending)
    paid = _ds(lake_trending, costly)
    r_free = run_backtest(BuyAndHold({"ticker": "UP.US"}), free, free.full_window)
    r_paid = run_backtest(BuyAndHold({"ticker": "UP.US"}), paid, paid.full_window)
    assert r_paid.final_return < r_free.final_return - 0.01  # the $100 fee alone is 1%
    # objectives score through the same path
    obj = FinalReturnObjective()
    assert obj.score(BuyAndHold({"ticker": "UP.US"}), paid) == pytest.approx(r_paid.final_return)


def test_costs_survive_dataset_copies(lake_trending):
    costly = CostModelSettings(default=AssetClassCosts(fee_flat=1.0))
    ds = dataclasses.replace(_ds(lake_trending, costly), train_end=date(2026, 1, 1))
    assert ds.costs == costly
