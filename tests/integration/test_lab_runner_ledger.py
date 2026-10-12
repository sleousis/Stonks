"""LabRunner and the trial ledger on a real lake: the stored winner, the
seeds each test used and the trials of a cancelled run (P2, BL-04, BL-06)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.config import Settings
from stonks.lab.dataset import LabDataset
from stonks.lab.heatmap import HeatmapOptions
from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.survival.registry import build_survival_test
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum

_SERIAL = ParallelSettings(max_workers=1)
CLASS_PATH = f"{Momentum.__module__}:{Momentum.__name__}"


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


@pytest.fixture
def ds(lake_trending):
    return LabDataset(
        lake=lake_trending,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
        costs=CostModelSettings(),
    )


def test_the_stored_winner_is_the_tuners_even_with_a_heatmap(ledger, ds):
    runner = LabRunner(
        tuner=RandomTuner(seed=4, parallel=_SERIAL),
        objective=SharpeObjective(),
        suite=SurvivalSuite([OutOfSampleTest()]),
        budget=6,
        ledger=ledger,
        settings=Settings(),
        heatmap=HeatmapOptions(x="lookback_days", y="threshold", grid_size=3, fast=False),
        parallel=_SERIAL,
    )
    result = runner.run(Momentum, ds)
    assert result.heatmap is not None
    run = ledger.run(result.run_id)
    assert run["best_params"] == result.best_params
    index = run["best_trial_index"]
    assert index is not None and index < 6  # a tuning trial, never a heatmap cell
    assert ledger.trials(result.run_id)[index].score == pytest.approx(result.best_score)


def test_the_recorded_mc_trades_seed_is_the_one_it_used(ledger, ds):
    mc = build_survival_test("mc_trades", {"min_trades": 1})
    result = LabRunner(
        tuner=RandomTuner(seed=11, parallel=_SERIAL),
        objective=SharpeObjective(),
        suite=SurvivalSuite([mc]),
        budget=2,
        ledger=ledger,
        settings=Settings(),
    ).run(Momentum, ds, fixed_params={"skip_days": 0})
    assert result.manifest["seeds"]["tests"]["mc_trades"] == 11
    assert ledger.run(result.run_id)["manifest"]["seeds"]["tests"]["mc_trades"] == 11


class _Cancelled(BaseException):
    pass


class _StopAfter(SharpeObjective):
    def __init__(self, n: int) -> None:
        super().__init__()
        self.n, self.calls = n, 0

    def evaluate(self, strategy, dataset):
        self.calls += 1
        if self.calls > self.n:
            raise _Cancelled()
        return super().evaluate(strategy, dataset)


def test_a_run_cancelled_after_8_of_10_trials_counts_at_least_8(ledger, ds):
    runner = LabRunner(
        GridTuner(grid_size=10, parallel=_SERIAL),
        _StopAfter(8),
        SurvivalSuite([OutOfSampleTest()]),
        budget=10,
        ledger=ledger,
        preflight=False,
    )
    with pytest.raises(_Cancelled):
        runner.run(Momentum, ds)
    assert ledger.n_trials(CLASS_PATH) >= 8
