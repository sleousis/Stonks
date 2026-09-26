"""Objective.evaluate: the full trial outcome (score plus per-bar returns)
that tuners record; ``score`` must stay ``evaluate(...).score``."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from stonks.core.protocols import TrialOutcome
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import CAGRObjective, FinalReturnObjective, SharpeObjective
from stonks.strategies.examples.buy_and_hold import BuyAndHold


def _dataset(lake):
    return LabDataset(
        lake=lake,
        universe=["UP.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.8,
    )


@pytest.mark.parametrize("objective", [SharpeObjective(), CAGRObjective(), FinalReturnObjective()])
def test_evaluate_returns_score_and_per_bar_returns(lake_trending, objective):
    ds = _dataset(lake_trending)
    strategy = BuyAndHold({"ticker": "UP.US", "allocation": 1.0})

    outcome = objective.evaluate(strategy, ds)

    assert isinstance(outcome, TrialOutcome)
    assert outcome.status == "ok"
    assert outcome.params == strategy.params
    assert outcome.score == objective.score(BuyAndHold(strategy.params), ds)

    report = run_backtest(BuyAndHold(strategy.params), ds, ds.train_window)
    curve = np.asarray(report.equity_curve)
    assert outcome.n_bars == len(curve) - 1
    np.testing.assert_allclose(outcome.returns, curve[1:] / curve[:-1] - 1.0)
    assert list(outcome.index) == list(
        np.array(report.equity_dates[1:], dtype="datetime64[ns]")
    )
    # returns cover the train window only
    assert outcome.index[-1] <= np.datetime64(ds.train_window[1])
