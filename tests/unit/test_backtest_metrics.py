"""Unit tests for the pure per-bar performance metrics (BL-03)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.backtest import metrics as m


def test_bar_returns_from_an_equity_curve():
    assert m.bar_returns([100.0, 110.0, 99.0]) == pytest.approx([0.10, -0.10])


def test_bar_returns_skip_steps_from_a_non_positive_value():
    assert m.bar_returns([100.0, 0.0, 50.0]) == pytest.approx([-1.0])


def test_sharpe_uses_sample_std_ddof_1():
    r = [0.01, -0.02, 0.03, 0.00]
    expected = np.mean(r) / np.std(r, ddof=1) * math.sqrt(252)
    assert m.sharpe(r, 252) == pytest.approx(expected)


def test_sharpe_of_a_constant_series_is_zero():
    assert m.sharpe([0.01] * 10, 252) == 0.0


def test_sharpe_needs_two_returns():
    assert m.sharpe([], 252) == 0.0
    assert m.sharpe([0.05], 252) == 0.0


def test_sharpe_subtracts_the_per_bar_risk_free_rate():
    r = [0.01, -0.02, 0.03, 0.00]
    rf = 0.0252  # annual; 0.0001 per bar at 252
    excess = np.array(r) - rf / 252
    expected = excess.mean() / np.std(excess, ddof=1) * math.sqrt(252)
    assert m.sharpe(r, 252, risk_free_rate=rf) == pytest.approx(expected)


def test_sortino_uses_downside_deviation_over_all_bars():
    r = [0.02, -0.01, 0.03, -0.02]
    downside = math.sqrt((0.01**2 + 0.02**2) / 4)
    assert m.sortino(r, 252) == pytest.approx(np.mean(r) / downside * math.sqrt(252))


def test_sortino_with_no_negative_returns():
    assert m.sortino([0.01, 0.02], 252) == math.inf
    assert m.sortino([0.0, 0.0], 252) == 0.0
    assert m.sortino([], 252) == 0.0


def test_drawdowns_and_max_drawdown():
    curve = [100.0, 90.0, 95.0, 100.0, 80.0, 110.0]
    assert m.drawdowns(curve) == pytest.approx([0.0, -0.1, -0.05, 0.0, -0.2, 0.0])
    assert m.max_drawdown(curve) == pytest.approx(-0.2)
    assert m.max_drawdown([]) == 0.0


def test_max_drawdown_duration_counts_the_longest_underwater_run():
    assert m.max_drawdown_duration_bars([100.0, 90.0, 95.0, 100.0, 80.0, 110.0]) == 2
    assert m.max_drawdown_duration_bars([100.0, 90.0, 80.0, 85.0]) == 3
    assert m.max_drawdown_duration_bars([100.0, 101.0, 102.0]) == 0


def test_ulcer_index_is_rms_drawdown():
    curve = [100.0, 90.0, 95.0, 100.0, 80.0, 110.0]
    expected = math.sqrt((0.1**2 + 0.05**2 + 0.2**2) / 6)
    assert m.ulcer_index(curve) == pytest.approx(expected)
    assert m.ulcer_index([]) == 0.0


def test_ratio_conventions_for_calmar_and_upi():
    assert m.calmar(0.2, -0.1) == pytest.approx(2.0)
    assert m.calmar(0.2, 0.0) == math.inf
    assert m.calmar(0.0, 0.0) == 0.0
    assert m.upi(0.2, 0.05) == pytest.approx(4.0)
    assert m.upi(-0.1, 0.0) == -math.inf


def test_historical_var_and_es_at_95():
    r = [float(x) for x in range(-10, 10)]  # -10 .. 9, 20 values
    var = float(np.quantile(r, 0.05))
    assert m.value_at_risk(r) == pytest.approx(var)
    assert m.expected_shortfall(r) == pytest.approx(np.mean([x for x in r if x <= var]))
    assert m.value_at_risk([]) == 0.0
    assert m.expected_shortfall([]) == 0.0


def test_skew_and_kurtosis_of_a_known_series():
    r = np.array([1.0, 2.0, 3.0, 10.0])
    d = r - r.mean()
    m2, m3, m4 = (d**2).mean(), (d**3).mean(), (d**4).mean()
    assert m.skew(r) == pytest.approx(m3 / m2**1.5)
    assert m.kurtosis(r) == pytest.approx(m4 / m2**2)


def test_kurtosis_is_about_three_on_a_large_normal_sample():
    r = np.random.default_rng(7).normal(0.0, 0.01, 200_000)
    assert m.kurtosis(r) == pytest.approx(3.0, abs=0.05)
    assert m.skew(r) == pytest.approx(0.0, abs=0.02)


def test_undefined_moments_fall_back_to_the_normal_values():
    assert m.skew([0.01] * 5) == 0.0
    assert m.kurtosis([0.01] * 5) == 3.0
    assert m.kurtosis([]) == 3.0


def test_lag_one_autocorrelation():
    r = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    assert m.autocorr_1(r) == pytest.approx(-1.0)
    assert m.autocorr_1([0.01] * 5) == 0.0
    assert m.autocorr_1([0.01, 0.02]) == 0.0


def test_lower_tail_ratio_is_about_one_for_a_normal_sample():
    r = np.random.default_rng(11).normal(0.001, 0.01, 200_000)
    assert m.lower_tail_ratio(r) == pytest.approx(1.0, abs=0.03)


def test_lower_tail_ratio_of_a_known_series():
    r = np.array([-5.0, -1.0, 0.0, 1.0, 2.0, 3.0])
    d = r - r.mean()
    expected = (np.quantile(d, 0.01) / np.quantile(d, 0.30)) / (2.326 / 0.524)
    assert m.lower_tail_ratio(r) == pytest.approx(expected)
    assert m.lower_tail_ratio([]) == 1.0


def test_fitness_floors_turnover():
    assert m.fitness(2.0, 0.16, 0.01) == pytest.approx(2.0 * math.sqrt(0.16 / 0.125))
    assert m.fitness(2.0, -0.16, 0.5) == pytest.approx(2.0 * math.sqrt(0.16 / 0.5))


def test_fitness_with_zero_sharpe_and_infinite_cagr_is_zero_not_nan():
    assert m.fitness(0.0, math.inf, 0.2) == 0.0
    assert m.fitness(1.0, math.inf, 0.2) == math.inf


def test_profit_factor_conventions():
    assert m.profit_factor([1.0, -0.5, 2.0]) == pytest.approx(6.0)
    assert m.profit_factor([1.0]) == math.inf
    assert m.profit_factor([-1.0, 0.0]) == 0.0
    assert m.profit_factor([]) == 0.0


def test_years_spanned_and_cagr():
    from datetime import datetime, timedelta

    t0 = datetime(2024, 1, 1)
    years = m.years_spanned([t0, t0 + timedelta(days=365.25)])
    assert years == pytest.approx(1.0)
    assert m.cagr(100.0, 121.0, 2.0) == pytest.approx(0.1)
    assert m.cagr(100.0, 0.0, 1.0) == -1.0
    assert m.cagr(0.0, 10.0, 1.0) == 0.0
