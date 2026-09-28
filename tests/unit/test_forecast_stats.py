"""Forecast evaluation statistics (roadmap 23.11)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.stats.forecast_tests import (
    crps_from_quantiles,
    diebold_mariano,
    mase,
    pinball_loss,
    rank_ic,
)


def test_dm_detects_a_clearly_better_model():
    rng = np.random.default_rng(0)
    y = rng.normal(0, 1, 500)
    base = y**2  # predicts 0
    model = (y - 0.8 * y + rng.normal(0, 0.1, 500)) ** 2  # tracks most of y
    dm = diebold_mariano(base, model, horizon=1)
    assert dm.mean_diff > 0
    assert dm.p_value < 1e-6
    assert dm.n == 500


def test_dm_equal_losses_give_no_evidence():
    rng = np.random.default_rng(1)
    a = rng.normal(1, 0.2, 300) ** 2
    dm = diebold_mariano(a, a.copy())
    assert dm.p_value == 1.0
    assert dm.stat == 0.0


def test_dm_is_one_sided_and_uses_at_least_h_minus_1_lags():
    rng = np.random.default_rng(2)
    base = rng.normal(1, 0.1, 200)
    worse = base + 0.5
    dm = diebold_mariano(base, worse, horizon=10)
    assert dm.p_value > 0.99
    assert dm.lags >= 9


def test_dm_drops_non_finite_pairs_and_needs_two():
    dm = diebold_mariano([1.0, np.nan, 2.0, 3.0], [0.5, 1.0, np.inf, 1.0])
    assert dm.n == 2
    with pytest.raises(ValueError):
        diebold_mariano([1.0], [0.0])


def test_mase_scales_by_the_naive_error():
    assert mase([1.0, -2.0], [2.0, 2.0]) == pytest.approx(0.75)
    assert mase([1.0, 5.0], [2.0, 0.0]) == pytest.approx(0.5)  # zero scales skipped
    assert math.isnan(mase([], []))


def test_pinball_and_crps():
    levels = (0.1, 0.5, 0.9)
    q = np.array([[-1.0, 0.0, 1.0]])
    assert pinball_loss(levels, q, np.array([0.0])) == pytest.approx((0.1 + 0 + 0.1) / 3)
    # a point mass at the truth scores zero, a wider band scores more
    assert crps_from_quantiles(levels, np.zeros((1, 3)), np.array([0.0])) == 0.0
    wide = crps_from_quantiles(levels, q * 3, np.array([0.0]))
    assert wide > crps_from_quantiles(levels, q, np.array([0.0]))


def test_crps_of_a_calibrated_normal_beats_a_biased_one():
    rng = np.random.default_rng(3)
    y = rng.normal(0, 1, 2000)
    levels = (0.1, 0.25, 0.5, 0.75, 0.9)
    z = np.array([-1.2816, -0.6745, 0.0, 0.6745, 1.2816])
    good = np.tile(z, (2000, 1))
    bad = good + 1.0
    assert crps_from_quantiles(levels, good, y) < crps_from_quantiles(levels, bad, y)


def test_rank_ic_per_date_then_averaged():
    frame = pd.DataFrame(
        {
            "date": ["d1"] * 4 + ["d2"] * 4 + ["d3"] * 2,
            "pred": [1, 2, 3, 4, 1, 2, 3, 4, 1, 2],
            "real": [1, 2, 3, 4, 4, 3, 2, 1, 1, 2],
        }
    )
    ic, n = rank_ic(frame["date"], frame["pred"], frame["real"])
    assert n == 2  # d3 has too few names
    assert ic == pytest.approx(0.0)
    ic, n = rank_ic(frame["date"][:4], frame["pred"][:4], frame["real"][:4])
    assert ic == pytest.approx(1.0)
    ic, n = rank_ic(frame["date"][8:], frame["pred"][8:], frame["real"][8:])
    assert n == 0
    assert math.isnan(ic)
