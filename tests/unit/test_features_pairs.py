"""Pairs features, checked by hand."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.features.pairs import half_life, hedge_ratio, pair_state, spread, spread_zscores


def test_hedge_ratio_recovers_the_slope():
    log_b = np.log(np.linspace(10.0, 20.0, 50))
    assert hedge_ratio(2.0 * log_b + 0.3, log_b) == pytest.approx(2.0)


def test_hedge_ratio_needs_variation():
    assert hedge_ratio(np.ones(5), np.ones(5)) is None
    assert hedge_ratio(np.ones(2), np.ones(2)) is None


def test_spread_and_zscores():
    s = spread(np.array([3.0, 5.0, 7.0]), np.array([1.0, 2.0, 3.0]), 2.0)
    assert s == pytest.approx([1.0, 1.0, 1.0])
    assert spread_zscores(np.array([1.0, 2.0, 3.0])) == pytest.approx([-1.0, 0.0, 1.0])
    assert spread_zscores(np.array([1.0, 1.0, 1.0])) is None


def test_half_life_of_a_halving_spread_is_one_bar():
    # s_t = 0.5 * s_{t-1}: ds = -0.5 * s_{t-1}, so b = -0.5 and the half-life is 1
    assert half_life(np.array([8.0, 4.0, 2.0, 1.0, 0.5])) == pytest.approx(1.0)


def test_half_life_of_a_slow_spread():
    # phi = 0.9: -ln 2 / ln 0.9
    s = 10.0 * 0.9 ** np.arange(30)
    assert half_life(s) == pytest.approx(-math.log(2) / math.log(0.9))


def test_a_trending_spread_does_not_revert():
    assert half_life(np.arange(1.0, 10.0)) is None


def test_pair_state_enters_exits_and_stops():
    # rich A (z high): short A, long B
    assert pair_state(np.array([0.0, 2.1]), 2.0, 0.5, 4.0) == -1
    # cheap A: long A
    assert pair_state(np.array([0.0, -2.5, -1.0]), 2.0, 0.5, 4.0) == 1
    # back inside the exit band: flat
    assert pair_state(np.array([2.5, 1.0, 0.3]), 2.0, 0.5, 4.0) == 0
    # a blow-out past the stop closes the trade
    assert pair_state(np.array([2.5, 4.2]), 2.0, 0.5, 4.0) == 0
    # never entered
    assert pair_state(np.array([1.0, -1.5]), 2.0, 0.5, 4.0) == 0


def test_pair_state_checks_its_bands():
    with pytest.raises(ValueError):
        pair_state(np.array([0.0]), 2.0, 2.5, 4.0)
