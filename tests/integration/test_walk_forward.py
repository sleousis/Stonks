"""WalkForwardTest: re-tune per fold on train only, score on test only."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.core.protocols import TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite, TuningSetup
from stonks.lab.survival.walk_forward import (
    WalkForwardConfig,
    WalkForwardTest,
    walk_forward_folds,
)
from stonks.lab.tuning.grid import GridTuner
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.momentum import Momentum

UNIVERSE = ["UP.US", "DOWN.US", "FLAT.US"]


def _ds(lake):
    return LabDataset(
        lake=lake, universe=UNIVERSE, start=date(2025, 10, 1), end=date(2026, 4, 1), train_ratio=0.6
    )


class _Recorder(BaseStrategy):
    """Records every as_of it is scored on and every dataset it is fit on."""

    id = "wf_recorder"
    as_ofs: list = []
    fitted_on: list = []

    def fit(self, dataset):
        _Recorder.fitted_on.append(dataset)

    def estimate_return(self, ticker, as_of, lake):
        _Recorder.as_ofs.append(pd.Timestamp(as_of).date())
        return

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


class _RecordingTuner:
    def __init__(self) -> None:
        self.datasets: list[LabDataset] = []

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        self.datasets.append(dataset)
        return TunerResult(best_params={}, best_score=float(len(self.datasets)), history=[])


def test_each_fold_tunes_on_its_train_window_and_scores_its_test_window(lake_trending):
    ds = _ds(lake_trending)
    cfg = WalkForwardConfig(n_splits=3, test_days=20, train_days=60)
    folds = walk_forward_folds(ds.start, ds.end, n_splits=3, test_days=20, train_days=60)
    tuner = _RecordingTuner()
    _Recorder.as_ofs, _Recorder.fitted_on = [], []

    report = WalkForwardTest(cfg, TuningSetup(tuner, SharpeObjective(), budget=2)).run(
        _Recorder({}), ds
    )

    assert [d.train_window for d in tuner.datasets] == [(f.train_start, f.train_end) for f in folds]
    assert [d.val_window for d in tuner.datasets] == [(f.test_start, f.test_end) for f in folds]
    assert _Recorder.fitted_on == tuner.datasets  # fitted on the fold, not the full dataset
    # scored only inside the test windows, and inside every one of them
    in_test = [any(f.test_start <= a <= f.test_end for f in folds) for a in _Recorder.as_ofs]
    assert _Recorder.as_ofs and all(in_test)
    for f in folds:
        assert any(f.test_start <= a <= f.test_end for a in _Recorder.as_ofs)
    assert min(_Recorder.as_ofs) >= folds[0].test_start
    # per-fold metrics (in-sample score is the tuner's best score)
    assert [report.metrics[f"fold{i}_is_score"] for i in range(3)] == [1.0, 2.0, 3.0]
    assert report.metrics["n_folds"] == 3.0
    assert report.metrics["fold2_oos_score"] == 0.0  # recorder never trades


def test_never_trading_strategy_fails(lake_trending):
    report = WalkForwardTest(
        WalkForwardConfig(n_splits=2, test_days=30),
        TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1),
    ).run(_Recorder({}), _ds(lake_trending))
    assert report.metrics["positive_share"] == 0.0
    assert report.passed is False


def test_thresholds_decide_pass_or_fail(lake_trending):
    ds = _ds(lake_trending)
    setup = TuningSetup(GridTuner(grid_size=2), SharpeObjective(), budget=4)
    strategy = Momentum({"lookback_days": 10, "threshold": 0.0})

    def run(**thresholds):
        cfg = WalkForwardConfig(n_splits=2, test_days=30, **thresholds)
        return WalkForwardTest(cfg, setup).run(strategy, ds)

    lenient = run(min_positive_share=0.0, min_mean_score=-1e9)
    strict = run(min_positive_share=0.0, min_mean_score=1e9)
    assert lenient.passed is True
    assert strict.passed is False
    assert lenient.metrics["oos_score_mean"] == strict.metrics["oos_score_mean"]
    # momentum rides UP.US: every fold is profitable out of sample
    assert lenient.metrics["positive_share"] == 1.0
    assert lenient.metrics["oos_score_mean"] > 0


def test_requires_a_tuning_setup(lake_trending):
    with pytest.raises(ValueError, match="tuning"):
        WalkForwardTest().run(Momentum({}), _ds(lake_trending))


def test_runs_inside_the_lab_runner_with_the_runners_tuner(lake_trending):
    runner = LabRunner(
        tuner=GridTuner(grid_size=2),
        objective=SharpeObjective(),
        suite=SurvivalSuite(
            tests=[
                WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30, min_positive_share=0.0))
            ]
        ),
        budget=4,
    )
    result = runner.run(strategy_cls=Momentum, dataset=_ds(lake_trending))
    (report,) = result.survival_reports
    assert report.test_id == "walk_forward"
    assert report.metrics["n_folds"] == 2.0
