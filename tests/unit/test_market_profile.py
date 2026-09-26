"""Unit tests for the market-profile helpers (weighted KDE, peak
prominence, support/resistance levels)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.features.market_profile import (
    find_peaks,
    market_profile_levels,
    peak_prominences,
    weighted_gaussian_kde,
)


def test_find_peaks_simple():
    y = np.array([0, 2, 1, 3, 0, 1, 0.0])
    assert find_peaks(y).tolist() == [1, 3, 5]


def test_find_peaks_ignores_edges_and_takes_plateau_middle():
    y = np.array([5, 1, 2, 2, 2, 1, 4.0])
    assert find_peaks(y).tolist() == [3]


def test_peak_prominences_hand_computed():
    y = np.array([0, 2, 1, 3, 0, 1, 0.0])
    # peak 1: left base 0, right base 1 (stops at the higher peak 3) -> 2-1
    # peak 3: bases 0 and 0 -> 3; peak 5: bases 0 / 0 -> 1
    assert peak_prominences(y, np.array([1, 3, 5])).tolist() == [1.0, 3.0, 1.0]


def test_find_peaks_min_prominence_filters():
    y = np.array([0, 2, 1, 3, 0, 1, 0.0])
    assert find_peaks(y, min_prominence=1.5).tolist() == [3]


def test_weighted_kde_integrates_to_one_and_centres_on_mass():
    rng = np.random.default_rng(0)
    x = rng.normal(0.0, 1.0, 500)
    grid = np.linspace(-6, 6, 2001)
    dens = weighted_gaussian_kde(x, np.ones_like(x), 0.3, grid)
    assert np.trapezoid(dens, grid) == pytest.approx(1.0, abs=1e-3)
    assert abs(grid[np.argmax(dens)]) < 0.3


def test_weighted_kde_weights_shift_the_mode():
    x = np.array([0.0] * 10 + [5.0] * 10)
    w = np.array([1.0] * 10 + [0.1] * 10)
    grid = np.linspace(-2, 7, 901)
    dens = weighted_gaussian_kde(x, w, 0.1, grid)
    assert grid[np.argmax(dens)] == pytest.approx(0.0, abs=0.05)


def test_market_profile_levels_find_both_modes_of_a_bimodal_sample():
    rng = np.random.default_rng(1)
    prices = np.concatenate([rng.normal(100, 0.5, 200), rng.normal(120, 0.5, 200)])
    rng.shuffle(prices)
    levels = market_profile_levels(
        np.log(prices), log_atr=0.01, first_w=1.0, atr_mult=3.0, prom_thresh=0.25
    )
    assert len(levels) == 2
    assert levels[0] == pytest.approx(100, abs=1.5)
    assert levels[1] == pytest.approx(120, abs=1.5)


def test_market_profile_levels_constant_prices_give_nothing():
    assert market_profile_levels(np.log(np.full(50, 100.0)), 0.01) == []


def test_market_profile_levels_rejects_nan_atr():
    assert market_profile_levels(np.log(np.linspace(90, 110, 50)), float("nan")) == []
