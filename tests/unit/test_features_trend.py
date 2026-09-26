"""Clenow trend features: regression momentum and the gap filter."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.features.trend import max_gap, regression_momentum


def test_regression_momentum_on_a_clean_exponential_series():
    daily = 0.001
    closes = 50 * np.exp(daily * np.arange(90))
    fit = regression_momentum(closes, lookback=90, annualization=250)
    assert fit.r_squared == pytest.approx(1.0)
    assert fit.annualized_slope == pytest.approx(math.exp(daily * 250) - 1)
    assert fit.score == pytest.approx(math.exp(daily * 250) - 1)


def test_regression_momentum_uses_only_the_last_lookback_closes():
    closes = np.concatenate([np.full(50, 1.0), 50 * np.exp(0.002 * np.arange(90))])
    fit = regression_momentum(closes, lookback=90, annualization=250)
    assert fit.r_squared == pytest.approx(1.0)


def test_noise_lowers_the_score_through_r_squared():
    rng = np.random.default_rng(1)
    clean = 50 * np.exp(0.002 * np.arange(90))
    noisy = clean * np.exp(rng.normal(0, 0.03, 90))
    fit_clean = regression_momentum(clean, 90, 250)
    fit_noisy = regression_momentum(noisy, 90, 250)
    assert fit_noisy.r_squared < fit_clean.r_squared
    assert fit_noisy.score < fit_clean.score


def test_declining_series_scores_negative():
    closes = 50 * np.exp(-0.002 * np.arange(90))
    assert regression_momentum(closes, 90, 250).score < 0


def test_flat_series_scores_zero():
    fit = regression_momentum(np.full(90, 10.0), 90, 250)
    assert fit.score == 0.0


def test_regression_momentum_needs_lookback_positive_closes():
    assert regression_momentum(np.ones(89), 90, 250) is None
    bad = np.ones(90)
    bad[3] = -1.0
    assert regression_momentum(bad, 90, 250) is None


def test_max_gap_is_the_largest_absolute_daily_move():
    closes = np.array([100.0, 101.0, 85.0, 86.0, 99.0])
    assert max_gap(closes, lookback=4) == pytest.approx(0.1584158, rel=1e-6)
    # only the last two moves are inside a 2-bar window
    assert max_gap(closes, lookback=2) == pytest.approx(13 / 86)


def test_max_gap_needs_lookback_returns():
    assert max_gap(np.array([1.0, 2.0]), lookback=2) is None
