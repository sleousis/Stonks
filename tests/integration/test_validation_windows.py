"""BL-21: validation-style survival tests score the (embargoed) validation
window by default, never the tuned window; ``window="full"`` reproduces the
pre-BL-21 behaviour. Perturbation runs are parallel; the MCPT defaults to
200 permutations."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.lab.dataset import LabDataset
from stonks.lab.survival import period_stability as ps_mod
from stonks.lab.survival import perturbation as pt_mod
from stonks.lab.survival import runs_test as rt_mod
from stonks.lab.survival.period_stability import PeriodStabilityTest
from stonks.lab.survival.permutation import MonteCarloPermutationTest, has_nontrivial_fit
from stonks.lab.survival.perturbation import PerturbationTest
from stonks.lab.survival.runs_test import RunsTestSurvivalTest
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum


def _ds(lake, universe=("UP.US",), **kw):
    return LabDataset(
        lake=lake,
        universe=list(universe),
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
        **kw,
    )


def _record_windows(monkeypatch, module):
    windows: list = []
    real = module.run_backtest

    def recording(strategy, dataset, window, lake=None, **kw):
        windows.append(tuple(window))
        return real(strategy, dataset, window, lake, **kw)

    monkeypatch.setattr(module, "run_backtest", recording)
    return windows


class _Labelled(BuyAndHold):
    label_horizon_bars = 15


UP = {"ticker": "UP.US", "allocation": 1.0}


# ---- runs test ------------------------------------------------------------------


def test_runs_test_scores_the_validation_window_by_default(lake_trending, monkeypatch):
    windows = _record_windows(monkeypatch, rt_mod)
    ds = _ds(lake_trending)
    report = RunsTestSurvivalTest().run(BuyAndHold(UP), ds)
    assert windows == [ds.val_window]
    assert "window=val" in report.notes


def test_runs_test_full_window_reproduces_the_old_behaviour(lake_trending, monkeypatch):
    windows = _record_windows(monkeypatch, rt_mod)
    ds = _ds(lake_trending)
    RunsTestSurvivalTest(window="full").run(BuyAndHold(UP), ds)
    assert windows == [ds.full_window]


def test_runs_test_uses_the_strategy_embargo(lake_trending, monkeypatch):
    windows = _record_windows(monkeypatch, rt_mod)
    ds = _ds(lake_trending)
    RunsTestSurvivalTest().run(_Labelled(UP), ds)
    assert windows == [ds.for_strategy(_Labelled(UP)).val_window]
    assert windows[0][0] > ds.val_window[0]


def test_runs_test_rejects_an_unknown_window():
    with pytest.raises(ValueError):
        RunsTestSurvivalTest(window="train")


# ---- period stability ---------------------------------------------------------------


def test_period_stability_sub_windows_cover_the_validation_window(lake_trending, monkeypatch):
    windows = _record_windows(monkeypatch, ps_mod)
    ds = _ds(lake_trending)
    PeriodStabilityTest(n_windows=3, max_sharpe_std=1e9).run(BuyAndHold(UP), ds)
    val_start, val_end = ds.val_window
    assert len(windows) == 3
    assert windows[0][0] == val_start
    assert windows[-1][1] == val_end
    assert all(w[0] >= val_start for w in windows)
    for prev, nxt in zip(windows, windows[1:], strict=False):
        assert prev[1] <= nxt[0]


def test_period_stability_full_window_reproduces_the_old_split(lake_trending, monkeypatch):
    windows = _record_windows(monkeypatch, ps_mod)
    ds = _ds(lake_trending)
    PeriodStabilityTest(n_windows=3, max_sharpe_std=1e9, window="full").run(BuyAndHold(UP), ds)
    chunk = (ds.end - ds.start).days // 3
    assert windows == [
        (ds.start, ds.start + timedelta(days=chunk)),
        (ds.start + timedelta(days=chunk), ds.start + timedelta(days=2 * chunk)),
        (ds.start + timedelta(days=2 * chunk), ds.end),
    ]


def test_period_stability_on_too_short_a_window_fails(lake_trending):
    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_end=date(2026, 3, 30),
    )
    report = PeriodStabilityTest(n_windows=3).run(BuyAndHold(UP), ds)
    assert report.passed is False
    assert "insufficient data" in report.notes


# ---- perturbation ------------------------------------------------------------------------


def test_perturbation_scores_the_validation_window_by_default(lake_trending, monkeypatch):
    windows = _record_windows(monkeypatch, pt_mod)
    ds = _ds(lake_trending)
    report = PerturbationTest(noise_sigmas=[0.0, 0.01], min_correlation=-1.0).run(
        BuyAndHold(UP), ds
    )
    assert set(windows) == {ds.val_window}
    assert len(windows) == 2  # baseline + one noisy level (sigma 0 is not re-run)
    assert "window=val" in report.notes


def test_perturbation_full_window_reproduces_the_old_behaviour(lake_trending, monkeypatch):
    windows = _record_windows(monkeypatch, pt_mod)
    ds = _ds(lake_trending)
    PerturbationTest(noise_sigmas=[0.01], min_correlation=-1.0, window="full").run(
        BuyAndHold(UP), ds
    )
    assert set(windows) == {ds.full_window}


def test_perturbation_parallel_equals_serial(lake_trending):
    ds = _ds(lake_trending, universe=("UP.US", "DOWN.US", "FLAT.US"))
    strategy = Momentum({"lookback_days": 10, "threshold": 0.0})

    def run(workers):
        return PerturbationTest(
            noise_sigmas=[0.0, 0.01, 0.03], min_correlation=-1.0, seed=4, max_workers=workers
        ).run(strategy, ds)

    serial, parallel = run(1), run(2)
    assert serial.metrics == parallel.metrics
    assert serial.notes == parallel.notes


def test_perturbation_rejects_bad_options():
    with pytest.raises(ValueError):
        PerturbationTest(window="train")
    with pytest.raises(ValueError):
        PerturbationTest(max_workers=0)


# ---- MCPT ------------------------------------------------------------------------------------


def test_mcpt_defaults_to_200_permutations():
    test = MonteCarloPermutationTest()
    assert test._n == 200


class _Fitted(BaseStrategy):
    id = "mcpt_fitted"

    def fit(self, dataset):
        self.state = 1

    def estimate_return(self, ticker, as_of, lake):
        return

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


def test_nontrivial_fit_detection():
    assert has_nontrivial_fit(_Fitted({})) is True
    assert has_nontrivial_fit(BuyAndHold(UP)) is False
    assert has_nontrivial_fit(object()) is False


def test_mcpt_auto_retune_needs_a_setup_only_for_fitted_strategies(lake_trending):
    ds = _ds(lake_trending)
    auto = MonteCarloPermutationTest(n_permutations=2, max_p_value=1.0, retune="auto", seed=1)
    # rule-based: out-of-sample mode, no tuning setup needed
    report = auto.run(BuyAndHold(UP), ds)
    assert "mode=oos" in report.notes
    # fitted: re-tune mode, which needs the runner's setup
    with pytest.raises(ValueError, match="tuning setup"):
        auto.run(_Fitted({}), ds)


def test_mcpt_rejects_an_unknown_retune_value():
    with pytest.raises(ValueError):
        MonteCarloPermutationTest(retune="sometimes")


def test_mcpt_oos_mode_uses_the_strategy_embargo(lake_trending, monkeypatch):
    from stonks.lab.survival import permutation as perm_mod

    seen: list = []
    real_build = perm_mod.PermutationScorer.build

    def build(strategy, context, window, evaluate):
        seen.append(window)
        return real_build(strategy, context, window, evaluate)

    monkeypatch.setattr(perm_mod.PermutationScorer, "build", staticmethod(build))
    ds = _ds(lake_trending)
    MonteCarloPermutationTest(n_permutations=1, max_p_value=1.0).run(_Labelled(UP), ds)
    assert seen == [ds.for_strategy(_Labelled(UP)).val_window]
