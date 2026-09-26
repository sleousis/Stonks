"""Walk-forward upgrades (BL-20): embargo, stitched OOS scoring, walk-forward
efficiency, the train x test matrix and parallel folds."""

from __future__ import annotations

import dataclasses
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.report import BacktestReport, compute_report
from stonks.core.protocols import TunerResult
from stonks.lab.dataset import LabDataset, embargo_calendar_days
from stonks.lab.objectives import SharpeObjective
from stonks.lab.survival import walk_forward as wf
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.walk_forward import (
    WalkForwardConfig,
    WalkForwardTest,
    psr0,
    stitch_reports,
    stitched_returns,
    walk_forward_efficiency,
)
from stonks.lab.tuning.grid import GridTuner
from stonks.stats.sharpe import psr, return_moments
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.momentum import Momentum

UNIVERSE = ["UP.US", "DOWN.US", "FLAT.US"]


def _ds(lake, **kw):
    return LabDataset(
        lake=lake,
        universe=UNIVERSE,
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
        **kw,
    )


class _Idle(BaseStrategy):
    id = "wf_idle"

    def estimate_return(self, ticker, as_of, lake):
        return

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


class _Labelled(_Idle):
    id = "wf_labelled"
    label_horizon_bars = 10


class _RecordingTuner:
    def __init__(self) -> None:
        self.datasets: list = []

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        self.datasets.append(dataset)
        return TunerResult(best_params={}, best_score=0.0, history=[])


# ---- embargo -----------------------------------------------------------------------


def test_folds_keep_the_dataset_embargo_between_train_and_test(lake_trending):
    tuner = _RecordingTuner()
    ds = _ds(lake_trending, embargo_bars=5)
    cfg = WalkForwardConfig(n_splits=2, test_days=20, train_days=60, min_wfe=None)
    WalkForwardTest(cfg, TuningSetup(tuner, SharpeObjective(), budget=1)).run(_Idle({}), ds)

    gap = embargo_calendar_days(5, ds.interval)
    folds = cfg.folds_for(ds)
    for fold_ds, fold in zip(tuner.datasets, folds, strict=True):
        assert fold_ds.train_window == (fold.train_start, fold.train_end)
        assert fold.train_end + timedelta(days=1 + gap) == fold.test_start
        # the fold dataset's own validation window is exactly the test window
        assert fold_ds.val_window == (fold.test_start, fold.test_end)


def test_label_horizon_widens_the_fold_embargo(lake_trending):
    tuner = _RecordingTuner()
    ds = _ds(lake_trending, embargo_bars=2)
    cfg = WalkForwardConfig(n_splits=2, test_days=20, train_days=60, min_wfe=None)
    report = WalkForwardTest(cfg, TuningSetup(tuner, SharpeObjective(), budget=1)).run(
        _Labelled({}), ds
    )
    gap = embargo_calendar_days(10, ds.interval)
    for fold_ds in tuner.datasets:
        assert fold_ds.val_window[0] == fold_ds.train_window[1] + timedelta(days=1 + gap)
    assert f"embargo={gap}d" in report.notes


def test_nothing_in_the_embargo_is_scored(lake_trending, monkeypatch):
    windows: list = []
    real = wf.run_backtest

    def recording(strategy, dataset, window, lake=None):
        windows.append(window)
        return real(strategy, dataset, window, lake)

    monkeypatch.setattr(wf, "run_backtest", recording)
    ds = _ds(lake_trending, embargo_bars=5)
    cfg = WalkForwardConfig(n_splits=2, test_days=20, train_days=60, min_wfe=None)
    WalkForwardTest(cfg, TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)).run(
        _Idle({}), ds
    )
    folds = cfg.folds_for(ds)
    # per fold: an IS run on the train window and an OOS run on the test
    # window, neither touching that fold's embargo gap
    assert len(windows) == 2 * len(folds)
    for f, is_window, oos_window in zip(folds, windows[::2], windows[1::2], strict=True):
        gap = (f.train_end + timedelta(days=1), f.test_start - timedelta(days=1))
        assert gap[0] <= gap[1]
        assert is_window == (f.train_start, f.train_end)
        assert oos_window == (f.test_start, f.test_end)
        assert is_window[1] < gap[0] and oos_window[0] > gap[1]


# ---- stitching -----------------------------------------------------------------------


def _report(values, first_day):
    dates = [first_day + timedelta(days=i) for i in range(len(values))]
    return compute_report("s", dates, values)


def test_stitched_returns_are_the_concatenated_fold_returns():
    a = _report([100.0, 101.0, 99.0, 102.0], date(2025, 1, 1))
    b = _report([10_000.0, 10_050.0, 10_020.0, 10_100.0, 10_090.0], date(2025, 1, 5))
    stitched = stitch_reports([a, b])
    concat = stitched_returns([a, b])
    np.testing.assert_allclose(stitched.returns, concat, rtol=1e-12)
    assert stitched.equity_curve[0] == 100.0
    assert len(stitched.equity_curve) == 4 + 4  # b's first mark is dropped
    assert stitched.equity_dates[-1] == date(2025, 1, 9)


def test_stitched_psr_equals_the_psr_of_the_concatenated_series():
    rng = np.random.default_rng(3)
    reports = []
    for k in range(3):
        rets = rng.normal(0.001, 0.01, 40)
        curve = list(10_000.0 * np.cumprod(np.r_[1.0, 1 + rets]))
        reports.append(_report(curve, date(2025, 1, 1) + timedelta(days=60 * k)))
    concat = np.concatenate([np.asarray(r.returns) for r in reports])
    mom = return_moments(concat)
    expected = psr(mom.sharpe, 0.0, mom.n, mom.skew, mom.kurt, mom.rho)
    assert psr0(stitched_returns(reports)) == pytest.approx(expected, rel=1e-12)
    assert psr0(np.asarray(stitch_reports(reports).returns)) == pytest.approx(expected, rel=1e-9)


def test_stitched_trades_keep_their_return_on_equity(lake_trending):
    ds = _ds(lake_trending)
    setup = TuningSetup(GridTuner(grid_size=2), SharpeObjective(), budget=4)
    cfg = WalkForwardConfig(n_splits=2, test_days=30, min_wfe=None)
    report = WalkForwardTest(cfg, setup).run(Momentum({"lookback_days": 10}), ds)
    stitched = ds.stitched_oos_report
    assert isinstance(stitched, BacktestReport)
    assert report.metrics["psr0_stitched"] == pytest.approx(psr0(np.asarray(stitched.returns)))
    assert report.metrics["cagr_oos_stitched"] == stitched.cagr
    assert report.metrics["n_bars_stitched"] == len(stitched.equity_curve) - 1
    for key in ("sharpe_stitched", "psr0_stitched", "wfe", "is_cagr_mean"):
        assert key in report.metrics


# ---- walk-forward efficiency ------------------------------------------------------------


def test_walk_forward_efficiency_is_oos_over_mean_is():
    assert walk_forward_efficiency(0.2, [0.4, 0.4]) == pytest.approx(0.5)
    assert walk_forward_efficiency(0.2, [0.4, float("inf")]) == pytest.approx(0.5)
    assert math.isnan(walk_forward_efficiency(0.2, [-0.1, 0.05]))
    assert math.isnan(walk_forward_efficiency(0.2, []))


def _fake_backtests(monkeypatch, is_growth: float, oos_growth: float):
    """Replace backtests with smooth curves: IS windows compound
    ``is_growth`` per day, OOS windows ``oos_growth``."""

    def fake(strategy, dataset, window, lake=None):
        start, end = window
        days = pd.date_range(start, end, freq="D").date
        growth = is_growth if start == dataset.start else oos_growth
        wiggle = np.where(np.arange(len(days)) % 2 == 0, 1.0005, 0.9995)
        curve = list(10_000.0 * np.cumprod(np.full(len(days), growth) * wiggle))
        return compute_report(getattr(strategy, "id", "s"), list(days), curve)

    monkeypatch.setattr(wf, "run_backtest", fake)


def test_a_wfe_below_the_gate_fails(lake_trending, monkeypatch):
    _fake_backtests(monkeypatch, is_growth=1.004, oos_growth=1.0005)
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    ds = _ds(lake_trending)
    gated = WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30), setup).run(_Idle({}), ds)
    assert gated.metrics["positive_share"] == 1.0  # every fold made money ...
    assert 0.0 < gated.metrics["wfe"] < 0.5  # ... but kept too little of the IS return
    assert gated.passed is False
    assert "wfe" in gated.notes

    off = WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30, min_wfe=None), setup).run(
        _Idle({}), ds
    )
    assert off.passed is True
    assert off.metrics["wfe"] == gated.metrics["wfe"]


def test_a_wfe_above_the_gate_passes(lake_trending, monkeypatch):
    _fake_backtests(monkeypatch, is_growth=1.002, oos_growth=1.0018)
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    report = WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30), setup).run(
        _Idle({}), _ds(lake_trending)
    )
    assert report.metrics["wfe"] >= 0.5
    assert report.passed is True


def test_no_in_sample_gain_fails_the_wfe_gate(lake_trending, monkeypatch):
    _fake_backtests(monkeypatch, is_growth=0.999, oos_growth=1.001)
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    report = WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30), setup).run(
        _Idle({}), _ds(lake_trending)
    )
    assert math.isnan(report.metrics["wfe"])
    assert report.passed is False
    assert "in-sample return <= 0" in report.notes


# ---- matrix ------------------------------------------------------------------------------


def test_matrix_reports_every_fitting_cell(lake_trending, monkeypatch):
    _fake_backtests(monkeypatch, is_growth=1.002, oos_growth=1.0018)
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    cfg = WalkForwardConfig(
        n_splits=2,
        test_days=30,
        matrix=True,
        matrix_train_bars=(40, 60, 5000),
        matrix_test_bars=(10,),
    )
    report = WalkForwardTest(cfg, setup).run(_Idle({}), _ds(lake_trending))
    assert report.metrics["matrix_cells"] == 2.0  # 5000 bars do not fit
    assert report.metrics["matrix_40x10_passed"] == 1.0
    assert report.metrics["matrix_pass_share"] == 1.0
    assert report.passed is True


def test_matrix_below_its_pass_share_fails(lake_trending, monkeypatch):
    _fake_backtests(monkeypatch, is_growth=1.002, oos_growth=1.0018)
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    cfg = WalkForwardConfig(
        n_splits=2, test_days=30, matrix=True, matrix_train_bars=(9000,), matrix_test_bars=(10,)
    )
    report = WalkForwardTest(cfg, setup).run(_Idle({}), _ds(lake_trending))
    assert report.metrics["matrix_cells"] == 0.0
    assert report.passed is False
    assert "matrix" in report.notes


# ---- parallel folds ------------------------------------------------------------------------


def test_serial_and_parallel_folds_give_identical_reports(lake_trending):
    setup = TuningSetup(GridTuner(grid_size=2), SharpeObjective(), budget=4)
    strategy = Momentum({"lookback_days": 10, "threshold": 0.0})

    def run(workers):
        ds = _ds(lake_trending)
        cfg = WalkForwardConfig(n_splits=3, test_days=20, max_workers=workers)
        return WalkForwardTest(cfg, setup).run(strategy, ds), ds.stitched_oos_report

    serial, serial_stitched = run(1)
    parallel, parallel_stitched = run(2)
    assert serial.metrics == pytest.approx(parallel.metrics, nan_ok=True)
    assert {k: v for k, v in serial.metrics.items() if not math.isnan(v)} == {
        k: v for k, v in parallel.metrics.items() if not math.isnan(v)
    }
    assert serial.notes == parallel.notes
    assert serial_stitched.equity_curve == parallel_stitched.equity_curve
    assert dataclasses.astuple(serial_stitched.trade_stats) == dataclasses.astuple(
        parallel_stitched.trade_stats
    )


def test_mc_trades_after_walk_forward_scores_the_stitched_trades(lake_trending):
    from stonks.lab.survival.mc_trades import MonteCarloTradesTest

    ds = _ds(lake_trending)
    setup = TuningSetup(GridTuner(grid_size=2), SharpeObjective(), budget=4)
    strategy = Momentum({"lookback_days": 10})
    WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30), setup).run(strategy, ds)
    report = MonteCarloTradesTest(min_trades=1).run(strategy, ds)
    assert "stitched walk-forward OOS" in report.notes


def test_mc_trades_listed_before_walk_forward_still_scores_the_stitched_trades(lake_trending):
    from stonks.lab.survival.base import SurvivalSuite
    from stonks.lab.survival.mc_trades import MonteCarloTradesTest

    ds = _ds(lake_trending)
    setup = TuningSetup(GridTuner(grid_size=2), SharpeObjective(), budget=4)
    suite = SurvivalSuite(
        [
            MonteCarloTradesTest(min_trades=1),
            WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30), setup),
        ]
    )
    mc, walk = suite.run(Momentum({"lookback_days": 10}), ds)
    assert (mc.test_id, walk.test_id) == ("mc_trades", "walk_forward")  # order kept
    assert "stitched walk-forward OOS" in mc.notes


# ---- edge cases (RS-26, RS-34) ------------------------------------------------------------


def test_no_finite_fold_score_fails_even_with_lenient_gates(lake_trending, monkeypatch):
    real = wf.run_backtest

    def nan_score(strategy, dataset, window, lake=None):
        report = real(strategy, dataset, window, lake)
        return dataclasses.replace(report, sharpe=float("nan"))

    monkeypatch.setattr(wf, "run_backtest", nan_score)
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    cfg = WalkForwardConfig(n_splits=2, test_days=30, min_positive_share=0.0, min_wfe=None)
    report = WalkForwardTest(cfg, setup).run(_Idle({}), _ds(lake_trending))
    assert math.isnan(report.metrics["oos_score_mean"])
    assert report.passed is False
    assert "oos_score_mean" in report.notes


def test_all_folds_without_trades_fail(lake_trending):
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    report = WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30), setup).run(
        _Idle({}), _ds(lake_trending)
    )
    assert report.passed is False
    assert report.metrics["positive_share"] == 0.0


def test_a_dataset_too_short_for_the_folds_fails_without_crashing(lake_trending):
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    cfg = WalkForwardConfig(n_splits=10, test_days=60)
    report = WalkForwardTest(cfg, setup).run(_Idle({}), _ds(lake_trending))
    assert report.passed is False
    assert "insufficient data" in report.notes
    assert report.metrics["n_folds"] == 0.0


def test_the_permutation_variant_survives_a_short_dataset(lake_trending):
    from stonks.lab.survival.walk_forward_permutation import (
        WalkForwardPermutationConfig,
        WalkForwardPermutationTest,
    )

    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    cfg = WalkForwardPermutationConfig(
        walk_forward=WalkForwardConfig(n_splits=10, test_days=60), n_permutations=2
    )
    test = WalkForwardPermutationTest(cfg, tuning=setup)
    report = test.run(_Idle({}), _ds(lake_trending))
    assert report.passed is False
    assert "insufficient data" in report.notes


def test_a_crypto_fold_with_no_bars_is_a_failing_fold_not_a_crash(lake_trending):
    ds = LabDataset(
        lake=lake_trending,
        universe=["NOBARS.CC"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
    )
    setup = TuningSetup(_RecordingTuner(), SharpeObjective(), budget=1)
    report = WalkForwardTest(WalkForwardConfig(n_splits=2, test_days=30), setup).run(_Idle({}), ds)
    assert report.passed is False
