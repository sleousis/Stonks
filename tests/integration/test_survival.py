"""Integration tests for lab.survival — four strategy-agnostic tests.

Each SurvivalTest receives a Strategy instance + a LabDataset (context) and
returns a SurvivalReport with pass/fail + metrics. A SurvivalSuite composes
any number of them.
"""

from __future__ import annotations

from datetime import date

from stonks.core.protocols import SurvivalReport
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.drift import DriftTest
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.survival.period_stability import PeriodStabilityTest
from stonks.lab.survival.perturbation import PerturbationTest
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum


def _dataset(lake, universe=("UP.US", "DOWN.US", "FLAT.US")):
    return LabDataset(
        lake=lake,
        universe=list(universe),
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
    )


# ---- OOS --------------------------------------------------------------------


def test_oos_test_passes_on_uptrend(lake_trending):
    # Legacy flat-Sharpe rule; buy-and-hold closes no trades, so the trade
    # minimum is off.
    test = OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99, mode="sharpe", min_trades=0)
    report = test.run(
        strategy=BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("UP.US",)),
    )
    assert isinstance(report, SurvivalReport)
    assert report.test_id == "oos"
    assert report.passed is True
    assert "sharpe_oos" in report.metrics


def test_default_oos_gate_fails_buy_and_hold_for_lack_of_trades(lake_trending):
    report = OutOfSampleTest(max_drawdown_limit=-0.99).run(
        strategy=BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("UP.US",)),
    )
    assert report.passed is False
    assert report.metrics["n_trades"] == 0
    assert "insufficient trades: 0 < 20" in report.notes
    assert 0.0 <= report.metrics["psr0"] <= 1.0


def test_oos_test_fails_on_downtrend(lake_trending):
    test = OutOfSampleTest(min_sharpe=0.5, max_drawdown_limit=-0.2)
    report = test.run(
        strategy=BuyAndHold({"ticker": "DOWN.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("DOWN.US",)),
    )
    assert report.passed is False


# ---- period stability -------------------------------------------------------


def test_period_stability_test_produces_metrics(lake_trending):
    test = PeriodStabilityTest(n_windows=3, max_sharpe_std=1e9)
    report = test.run(
        strategy=BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("UP.US",)),
    )
    assert report.test_id == "period_stability"
    assert "sharpe_std" in report.metrics
    assert "n_windows" in report.metrics
    assert report.metrics["n_windows"] == 3


def test_period_stability_fails_consistently_losing_strategy(lake_trending):
    # Sharpes are all negative but tightly clustered: std alone would pass.
    report = PeriodStabilityTest(n_windows=3, max_sharpe_std=1e9).run(
        strategy=BuyAndHold({"ticker": "DOWN.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("DOWN.US",)),
    )
    assert report.metrics["sharpe_max"] < 0.0
    assert report.passed is False


def test_period_stability_fails_strategy_that_never_trades(lake_trending):
    # Target ticker is outside the universe -> flat equity, every Sharpe == 0.
    report = PeriodStabilityTest(n_windows=3, max_sharpe_std=1e9).run(
        strategy=BuyAndHold({"ticker": "NOPE.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("UP.US",)),
    )
    assert report.metrics["sharpe_std"] == 0.0
    assert report.passed is False


def test_period_stability_min_sharpe_threshold_is_configurable(lake_trending):
    ctx = _dataset(lake_trending, universe=("DOWN.US",))
    strategy = BuyAndHold({"ticker": "DOWN.US", "allocation": 1.0})
    report = PeriodStabilityTest(n_windows=3, max_sharpe_std=1e9, min_period_sharpe=-1e9).run(
        strategy=strategy, context=ctx
    )
    assert report.passed is True


def test_period_stability_passes_consistent_winner(lake_trending):
    report = PeriodStabilityTest(n_windows=3, max_sharpe_std=1e9).run(
        strategy=BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("UP.US",)),
    )
    assert report.passed is True


# ---- perturbation -----------------------------------------------------------


def test_perturbation_test_reports_correlation(lake_trending):
    test = PerturbationTest(noise_sigmas=[0.0, 0.01], min_correlation=-1.0, seed=123)
    report = test.run(
        strategy=BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("UP.US",)),
    )
    assert report.test_id == "perturbation"
    assert "correlation_mean" in report.metrics


# ---- drift ------------------------------------------------------------------


def test_drift_test_reports_psi(lake_trending):
    test = DriftTest(max_psi=1e9, sample_dates=8)
    report = test.run(
        strategy=Momentum({"lookback_days": 10, "threshold": 0.0}),
        context=_dataset(lake_trending),
    )
    assert report.test_id == "drift"
    assert "max_psi" in report.metrics
    assert "mean_psi" in report.metrics


# ---- suite ------------------------------------------------------------------


def test_survival_suite_runs_all_tests(lake_trending):
    suite = SurvivalSuite(
        tests=[
            OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99),
            PeriodStabilityTest(n_windows=2, max_sharpe_std=1e9),
        ]
    )
    reports = suite.run(
        strategy=BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        context=_dataset(lake_trending, universe=("UP.US",)),
    )
    assert len(reports) == 2
    assert {r.test_id for r in reports} == {"oos", "period_stability"}
