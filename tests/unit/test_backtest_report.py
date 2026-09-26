"""Unit tests for ``compute_report`` metric edge cases."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from stonks.backtest.report import compute_report, periods_per_year
from stonks.core.interval import Interval


def _dates(n: int, step: timedelta) -> list[datetime]:
    start = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)
    return [start + i * step for i in range(n)]


_CURVE = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0]


def test_sharpe_defaults_to_daily_252_annualization():
    dates = _dates(len(_CURVE), timedelta(days=1))
    default = compute_report("s", dates, _CURVE)
    explicit = compute_report("s", dates, _CURVE, periods_per_year=252)
    assert default.sharpe == pytest.approx(explicit.sharpe)


def test_sharpe_scales_with_sqrt_of_periods_per_year():
    dates = _dates(len(_CURVE), timedelta(minutes=5))
    daily = compute_report("s", dates, _CURVE, periods_per_year=252)
    five_min = compute_report("s", dates, _CURVE, periods_per_year=252 * 78)
    assert five_min.sharpe == pytest.approx(daily.sharpe * math.sqrt(78))


def test_periods_per_year_daily_is_252():
    assert periods_per_year(Interval.DAY_1) == 252


def test_periods_per_year_intraday_uses_equity_session():
    # 6.5-hour US regular session → 78 five-minute bars, 6.5 hourly bars/day
    assert periods_per_year(Interval.MIN_5) == pytest.approx(252 * 78)
    assert periods_per_year(Interval.HOUR_1) == pytest.approx(252 * 6.5)
    # bars longer than the session still count as one bar per trading day
    assert periods_per_year(Interval.HOUR_12) == pytest.approx(252)


def test_periods_per_year_weekly_and_longer():
    assert periods_per_year(Interval.WEEK_1) == pytest.approx(52)
    assert periods_per_year(Interval.MONTH_1) == pytest.approx(12)
    assert periods_per_year(Interval.YEAR_1) == pytest.approx(1)


def test_cagr_on_tiny_intraday_span_does_not_overflow():
    dates = _dates(2, timedelta(minutes=5))
    report = compute_report("s", dates, [100.0, 101.0])
    assert report.cagr == math.inf


def test_cagr_on_total_wipeout_is_minus_one():
    dates = _dates(3, timedelta(days=30))
    report = compute_report("s", dates, [100.0, 50.0, 0.0])
    assert report.cagr == -1.0


def test_cagr_wipeout_ranks_below_flat():
    dates = _dates(3, timedelta(days=30))
    wiped = compute_report("s", dates, [100.0, 50.0, 0.0])
    flat = compute_report("s", dates, [100.0, 100.0, 100.0])
    assert wiped.cagr < flat.cagr


def test_sharpe_is_the_ddof_1_sample_sharpe():
    # Changed in BL-03: the population std (ddof=0) overstated Sharpe by
    # sqrt(n / (n - 1)).
    from stonks.backtest import metrics

    dates = _dates(len(_CURVE), timedelta(days=1))
    report = compute_report("s", dates, _CURVE)
    assert report.sharpe == pytest.approx(metrics.sharpe(report.returns, 252))


def test_returns_property_is_derived_from_the_equity_curve():
    dates = _dates(3, timedelta(days=1))
    report = compute_report("s", dates, [100.0, 110.0, 99.0])
    assert report.returns == pytest.approx([0.10, -0.10])


def test_report_carries_every_return_series_metric():
    from stonks.backtest import metrics

    dates = _dates(len(_CURVE), timedelta(days=1))
    report = compute_report("s", dates, _CURVE, risk_free_rate=0.01)
    r = report.returns
    assert report.sharpe == pytest.approx(metrics.sharpe(r, 252, 0.01))
    assert report.sortino == pytest.approx(metrics.sortino(r, 252, 0.01))
    assert report.calmar == pytest.approx(report.cagr / abs(report.max_drawdown))
    assert report.ulcer_index == pytest.approx(metrics.ulcer_index(_CURVE))
    assert report.upi == pytest.approx(report.cagr / report.ulcer_index)
    assert report.max_dd_duration_bars == metrics.max_drawdown_duration_bars(_CURVE)
    assert report.var_95 == pytest.approx(metrics.value_at_risk(r))
    assert report.es_95 == pytest.approx(metrics.expected_shortfall(r))
    assert report.skew == pytest.approx(metrics.skew(r))
    assert report.kurtosis == pytest.approx(metrics.kurtosis(r))
    assert report.autocorr_1 == pytest.approx(metrics.autocorr_1(r))
    assert report.lower_tail_ratio == pytest.approx(metrics.lower_tail_ratio(r))
    assert report.n_bars == len(_CURVE)


def test_bar_profit_factor_and_its_deprecated_alias():
    dates = _dates(len(_CURVE), timedelta(days=1))
    report = compute_report("s", dates, _CURVE)
    r = report.returns
    expected = sum(x for x in r if x > 0) / abs(sum(x for x in r if x < 0))
    assert report.bar_profit_factor == pytest.approx(expected)
    assert report.profit_factor == report.bar_profit_factor


def test_empty_report_has_safe_defaults():
    report = compute_report("s", [], [])
    assert report.returns == []
    assert report.sortino == 0.0 and report.calmar == 0.0 and report.ulcer_index == 0.0
    assert report.kurtosis == 3.0 and report.skew == 0.0
    assert report.trades == ()
    assert report.trade_stats.n_trades == 0
    assert report.fitness is None


def test_report_without_trades_has_an_empty_ledger():
    dates = _dates(len(_CURVE), timedelta(days=1))
    report = compute_report("s", dates, _CURVE)
    assert report.trades == ()
    assert report.trade_stats.n_trades == 0
    assert report.fitness is None
