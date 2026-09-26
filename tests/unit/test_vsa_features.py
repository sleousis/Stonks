"""Unit tests for the VSA helpers (OLS and range/volume deviation)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.features.vsa import ols, range_volume_deviation


def test_ols_recovers_exact_line():
    slope, intercept, r = ols(np.array([1.0, 2, 3, 4]), np.array([3.0, 5, 7, 9]))
    assert slope == pytest.approx(2.0)
    assert intercept == pytest.approx(1.0)
    assert r == pytest.approx(1.0)


def test_ols_residual_hand_computed():
    x = np.array([0.0, 1, 2])
    y = np.array([0.0, 2, 1])
    slope, intercept, r = ols(x, y)
    # xbar=1 ybar=1; sxy=1, sxx=2, syy=2 -> slope .5, intercept .5
    assert slope == pytest.approx(0.5)
    assert intercept == pytest.approx(0.5)
    assert y[1] - (intercept + slope * x[1]) == pytest.approx(1.0)
    assert r == pytest.approx(1.0 / np.sqrt(2 * 2))


def test_ols_zero_variance_returns_nan():
    slope, _intercept, r = ols(np.ones(4), np.arange(4.0))
    assert np.isnan(slope) and np.isnan(r)


def _frame(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    volume = rng.uniform(500, 1500, n)
    # range proportional to volume + a little noise -> strong positive fit
    spread = volume / 1000 * 2.0 + rng.normal(0, 0.05, n)
    close = 100 + rng.normal(0, 0.1, n).cumsum()
    return pd.DataFrame(
        {"high": close + spread / 2, "low": close - spread / 2, "close": close, "volume": volume}
    )


def _dev(df: pd.DataFrame, n: int = 20) -> np.ndarray:
    return range_volume_deviation(df["high"], df["low"], df["close"], df["volume"], n)


def test_deviation_nan_during_warmup_then_finite():
    dev = _dev(_frame(80))
    assert np.isnan(dev[:40]).all()
    assert np.isfinite(dev[40:]).all()


def test_deviation_negative_on_high_volume_small_range_bar():
    df = _frame(80)
    df.loc[79, "volume"] = 3000.0
    mid = df.loc[79, "close"]
    df.loc[79, "high"] = mid + 0.05
    df.loc[79, "low"] = mid - 0.05
    assert _dev(df)[79] < -1.0


def test_deviation_zero_when_fit_is_not_positive():
    n = 80
    rng = np.random.default_rng(2)
    volume = rng.uniform(500, 1500, n)
    spread = 3.0 - volume / 1000  # negative relation
    close = np.full(n, 100.0)
    df = pd.DataFrame(
        {"high": close + spread / 2, "low": close - spread / 2, "close": close, "volume": volume}
    )
    assert (_dev(df)[40:] == 0.0).all()


def test_deviation_excludes_current_bar_from_fit():
    # the fit for bar i uses bars [i-n, i-1] only, so widening bar 79 moves
    # dev[79] up and leaves every earlier value alone.
    df = _frame(80)
    base = _dev(df)
    df2 = df.copy()
    df2.loc[79, "high"] += 1.0
    moved = _dev(df2)
    assert np.allclose(base[:79], moved[:79], equal_nan=True)
    assert moved[79] > base[79]


def test_deviation_is_causal():
    df = _frame(80)
    full = _dev(df)
    part = _dev(df.iloc[:60])
    assert np.allclose(full[:60], part, equal_nan=True)


def test_deviation_handles_zero_atr_without_inf():
    df = pd.DataFrame(
        {"high": [100.0] * 60, "low": [100.0] * 60, "close": [100.0] * 60, "volume": [1.0] * 60}
    )
    dev = _dev(df)
    assert not np.isinf(dev).any()
