"""Brier score and reliability (roadmap 23.9)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.stats.calibration import brier_score, expected_calibration_error, reliability


def test_brier_known_values() -> None:
    assert brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    # (0.8-1)^2 + (0.3-0)^2 + (0.6-0)^2 = 0.04 + 0.09 + 0.36
    assert brier_score([0.8, 0.3, 0.6], [1, 0, 0]) == pytest.approx(0.49 / 3)


def test_reliability_bins() -> None:
    bins = reliability([0.05, 0.15, 0.95, 1.0], [0, 1, 1, 1], n_bins=10)
    assert len(bins) == 10
    assert bins[0].count == 1 and bins[0].observed_rate == 0.0
    assert bins[1].mean_forecast == pytest.approx(0.15) and bins[1].observed_rate == 1.0
    assert bins[9].count == 2 and bins[9].mean_forecast == pytest.approx(0.975)
    assert bins[5].count == 0 and bins[5].mean_forecast is None
    # |0.05-0| + |0.15-1| + 2*|0.975-1| over 4
    assert expected_calibration_error(bins) == pytest.approx((0.05 + 0.85 + 0.05) / 4)


def test_a_calibrated_forecaster_has_small_error() -> None:
    rng = np.random.default_rng(0)
    p = rng.uniform(size=20_000)
    y = (rng.uniform(size=p.size) < p).astype(int)
    assert expected_calibration_error(reliability(p, y)) < 0.02


def test_empty_and_bad_input() -> None:
    assert expected_calibration_error(reliability([], [])) is None
    with pytest.raises(ValueError):
        brier_score([], [])
    with pytest.raises(ValueError):
        brier_score([1.2], [1])
    with pytest.raises(ValueError):
        brier_score([0.2], [2])
    with pytest.raises(ValueError):
        brier_score([0.2, 0.1], [1])
