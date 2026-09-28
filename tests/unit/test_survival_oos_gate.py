"""Unit tests for the statistical out-of-sample gate (BL-16).

The gate is judged from a ``BacktestReport`` (``OutOfSampleTest.evaluate``),
so these tests build reports from synthetic equity curves instead of
running backtests.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, timedelta

import numpy as np
import pytest

from stonks.backtest.report import BacktestReport, compute_report
from stonks.backtest.trades import TradeStats
from stonks.lab.survival import registry
from stonks.lab.survival.oos import OutOfSampleTest

PPY = 252.0


def _returns(sharpe_annual: float, n: int, sd: float = 0.01, seed: int = 1) -> np.ndarray:
    """``n`` per-bar returns whose sample Sharpe is exactly ``sharpe_annual``."""
    z = np.random.default_rng(seed).standard_normal(n)
    z = (z - z.mean()) / z.std(ddof=1)
    return sharpe_annual / math.sqrt(PPY) * sd + sd * z


def _report(returns, n_trades: int = 50, expectancy: float = 1.0) -> BacktestReport:
    curve = list(10_000.0 * np.cumprod(np.concatenate([[1.0], 1.0 + np.asarray(returns)])))
    dates = [date(2000, 1, 3) + timedelta(days=i) for i in range(len(curve))]
    report = compute_report("s", dates, curve, periods_per_year=PPY)
    return dataclasses.replace(
        report, trade_stats=TradeStats(n_trades=n_trades, expectancy=expectancy)
    )


def test_defaults_are_the_psr_gate():
    test = OutOfSampleTest()
    assert test.mode == "psr"
    assert test.min_psr == 0.95
    assert test.min_trades == 20
    assert test.min_sharpe == 0.5
    assert test.max_drawdown_limit == -0.3


def test_short_window_with_sharpe_0_6_fails_in_psr_mode():
    report = _report(_returns(0.6, 126))
    out = OutOfSampleTest(max_drawdown_limit=-1.0).evaluate(report)
    assert out.passed is False
    assert out.metrics["psr0"] < 0.95
    assert "psr" in out.notes.lower()


def test_long_window_with_sharpe_0_6_passes_in_psr_mode():
    report = _report(_returns(0.6, 252 * 20))
    out = OutOfSampleTest(max_drawdown_limit=-1.0).evaluate(report)
    assert out.metrics["psr0"] >= 0.95
    assert out.passed is True, out.notes


def test_same_sharpe_passes_legacy_mode_on_the_short_window():
    report = _report(_returns(0.6, 126))
    out = OutOfSampleTest(mode="sharpe", max_drawdown_limit=-1.0).evaluate(report)
    assert out.passed is True


def test_too_few_trades_fail_with_a_note():
    report = _report(_returns(2.0, 252 * 20), n_trades=5)
    out = OutOfSampleTest(max_drawdown_limit=-1.0).evaluate(report)
    assert out.passed is False
    assert out.metrics["n_trades"] == 5
    assert "5 < 20" in out.notes


def test_trade_minimum_also_applies_in_legacy_mode_unless_disabled():
    report = _report(_returns(2.0, 252), n_trades=5)
    assert OutOfSampleTest(mode="sharpe", max_drawdown_limit=-1.0).evaluate(report).passed is False
    legacy = OutOfSampleTest(mode="sharpe", min_trades=0, max_drawdown_limit=-1.0)
    assert legacy.evaluate(report).passed is True


@pytest.mark.parametrize(
    ("sharpe", "dd_limit"),
    [(0.4, -1.0), (0.6, -1.0), (0.6, -0.0001), (-0.5, -1.0)],
)
def test_legacy_mode_matches_the_old_rule(sharpe, dd_limit):
    report = _report(_returns(sharpe, 252))
    out = OutOfSampleTest(
        min_sharpe=0.5, max_drawdown_limit=dd_limit, mode="sharpe", min_trades=0
    ).evaluate(report)
    expected = report.sharpe >= 0.5 and report.max_drawdown >= dd_limit
    assert out.passed is bool(expected)


def test_drawdown_limit_applies_in_psr_mode():
    report = _report(_returns(1.0, 252 * 20))
    out = OutOfSampleTest(max_drawdown_limit=-0.0001).evaluate(report)
    assert out.passed is False
    assert "drawdown" in out.notes.lower()


def test_metrics_carry_the_inputs_and_the_ci_brackets_the_estimate():
    report = _report(_returns(1.0, 504), n_trades=30, expectancy=12.5)
    m = OutOfSampleTest().evaluate(report).metrics
    for key in (
        "sharpe_oos",
        "max_drawdown_oos",
        "final_return_oos",
        "cagr_oos",
        "sr_per_bar",
        "sharpe_se",
        "skew",
        "kurtosis",
        "rho",
        "n_bars",
        "psr0",
        "sharpe_ci_low",
        "sharpe_ci_high",
        "min_trl_bars",
        "n_trades",
        "trade_expectancy",
    ):
        assert key in m, key
    assert m["sharpe_ci_low"] <= m["sr_per_bar"] <= m["sharpe_ci_high"]
    assert m["sharpe_se"] > 0
    assert m["n_bars"] == 504
    assert m["trade_expectancy"] == 12.5
    assert m["sr_per_bar"] == pytest.approx(1.0 / math.sqrt(PPY), rel=1e-6)


def test_min_trl_is_infinite_for_a_losing_strategy():
    m = OutOfSampleTest().evaluate(_report(_returns(-0.5, 252))).metrics
    assert math.isinf(m["min_trl_bars"])


def test_too_few_bars_fail_with_insufficient_data_note():
    report = _report([0.01, 0.02], n_trades=100)
    for mode in ("psr", "sharpe"):
        out = OutOfSampleTest(mode=mode, min_sharpe=-10, max_drawdown_limit=-1.0).evaluate(report)
        assert out.passed is False
        assert "insufficient data" in out.notes


def test_registry_builds_with_options_and_rejects_bad_ones():
    test = registry.build_survival_test("oos", {"mode": "sharpe", "min_trades": 0})
    assert isinstance(test, OutOfSampleTest)
    assert test.mode == "sharpe" and test.min_trades == 0
    with pytest.raises(ValueError):
        registry.build_survival_test("oos", {"mode": "vibes"})
    with pytest.raises(ValueError):
        registry.build_survival_test("oos", {"min_psr": 1.5})
    with pytest.raises(ValueError):
        registry.build_survival_test("oos", {"min_trades": -1})


def test_positional_legacy_constructor_still_works():
    test = OutOfSampleTest(0.7, -0.2)
    assert test.min_sharpe == 0.7 and test.max_drawdown_limit == -0.2


# ---- edge cases ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["psr", "sharpe"])
def test_a_two_bar_validation_window_fails_for_insufficient_data(mode):
    report = OutOfSampleTest(mode=mode, min_trades=0).evaluate(_report([0.05]))
    assert report.passed is False
    assert "insufficient data" in report.notes
    assert report.metrics["n_bars"] == 1.0
    assert math.isnan(report.metrics["psr0"])


def _with_returns(report: BacktestReport, returns) -> object:
    """A duck-typed report whose ``returns`` hold ``returns`` (NaN included)."""
    from types import SimpleNamespace

    fields = {f.name: getattr(report, f.name) for f in dataclasses.fields(report)}
    return SimpleNamespace(**{**fields, "returns": list(returns)})


def test_nan_returns_are_dropped_not_propagated():
    clean = _returns(3.0, 500)
    gappy = _with_returns(_report(clean), [*clean[:100], float("nan"), *clean[100:], float("nan")])
    report = OutOfSampleTest().evaluate(gappy)
    base = OutOfSampleTest().evaluate(_report(clean))
    assert report.metrics["n_bars"] == 500.0
    assert report.metrics["psr0"] == pytest.approx(base.metrics["psr0"])
    assert math.isfinite(report.metrics["sharpe_ci_low"])
    assert math.isfinite(report.metrics["sharpe_ci_high"])


def test_all_nan_returns_fail():
    nan = float("nan")
    report = OutOfSampleTest().evaluate(_with_returns(_report([0.01] * 50), [nan] * 50))
    assert report.passed is False
    assert "insufficient data" in report.notes


def test_lot_figures_ride_on_the_report_without_changing_the_verdict():
    from stonks.portfolio.lots import LotReport

    base = _report(_returns(0.6, 252 * 20))
    lots = LotReport(profile="whole_shares", orders=10, skipped=2, min_capital=5_000.0)
    with_lots = dataclasses.replace(base, lots=lots)
    test = OutOfSampleTest(max_drawdown_limit=-1.0)
    out = test.evaluate(with_lots)
    assert out.metrics["min_capital"] == 5_000.0
    assert out.metrics["lot_skipped_orders"] == 2.0
    assert out.passed == test.evaluate(base).passed
