"""End-to-end lab.runner test: tune → fit → survival suite → verdict."""

from __future__ import annotations

from datetime import date

from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.tuning.grid import GridTuner
from stonks.strategies.examples.momentum import Momentum


def test_lab_runner_produces_a_verdict_and_artifacts(lake_trending):
    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
    )
    runner = LabRunner(
        tuner=GridTuner(grid_size=2),
        objective=SharpeObjective(),
        suite=SurvivalSuite(
            tests=[OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99)]
        ),
        budget=6,
    )

    result = runner.run(strategy_cls=Momentum, dataset=ds)

    assert result.best_params is not None
    assert result.strategy is not None
    assert isinstance(result.strategy, Momentum)
    assert len(result.survival_reports) == 1
    assert result.verdict in ("pass", "fail")
