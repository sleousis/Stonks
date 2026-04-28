"""Integration tests for lab.objectives: each objective must be strategy-
agnostic (scores any Strategy) and direction-correct."""

from __future__ import annotations

from datetime import date

from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import CAGRObjective, SharpeObjective
from stonks.strategies.examples.buy_and_hold import BuyAndHold


def test_sharpe_objective_prefers_uptrending(lake_trending):
    obj = SharpeObjective()
    assert obj.direction == "maximize"

    up_strategy = BuyAndHold({"ticker": "UP.US", "allocation": 1.0})
    down_strategy = BuyAndHold({"ticker": "DOWN.US", "allocation": 1.0})

    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US", "DOWN.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=1.0,  # use full window for train
    )

    s_up = obj.score(up_strategy, ds)
    s_down = obj.score(down_strategy, ds)
    assert s_up > s_down


def test_cagr_objective_positive_on_uptrend(lake_trending):
    obj = CAGRObjective()
    assert obj.direction == "maximize"

    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=1.0,
    )
    strategy = BuyAndHold({"ticker": "UP.US", "allocation": 1.0})
    score = obj.score(strategy, ds)
    assert score > 0
