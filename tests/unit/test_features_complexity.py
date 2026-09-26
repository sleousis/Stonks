"""Rolling complexity / reversibility features (features.complexity)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from scipy.special import rel_entr

from stonks.features import library
from stonks.features.complexity import (
    async_index,
    hvg_out_degrees,
    relative_async_index,
    rolling_permutation_entropy,
    rolling_ptsr,
    rolling_rai,
    rolling_runs_z,
)

# ---- permutation entropy -------------------------------------------------------


def test_rolling_permutation_entropy_wraps_the_library_function():
    rng = np.random.default_rng(0)
    arr = rng.normal(size=300)
    out = rolling_permutation_entropy(arr, window=60, d=3)  # 60 = 3! * 10
    np.testing.assert_allclose(out, library.permutation_entropy(arr, d=3, lookback_mult=10))


def test_rolling_permutation_entropy_is_zero_on_a_monotone_series():
    out = rolling_permutation_entropy(np.arange(100.0), window=24, d=3)
    assert np.isnan(out[0])
    assert out[-1] == pytest.approx(0.0)


def test_rolling_permutation_entropy_rounds_the_window_to_a_multiple_of_d_factorial():
    rng = np.random.default_rng(1)
    arr = rng.normal(size=200)
    np.testing.assert_allclose(
        rolling_permutation_entropy(arr, window=62, d=3),
        rolling_permutation_entropy(arr, window=60, d=3),
    )


# ---- PTSR ----------------------------------------------------------------------


def _kl_from_counts(window: np.ndarray, d: int) -> float:
    fac = math.factorial(d)
    fwd = library.ordinal_patterns(window, d)[d - 1 :].astype(int)
    rev = library.ordinal_patterns(window[::-1], d)[d - 1 :].astype(int)
    p_f = np.bincount(fwd, minlength=fac) / len(fwd)
    p_r = np.bincount(rev, minlength=fac) / len(rev)
    if min(p_f.min(), p_r.min()) <= 0:
        return float("nan")
    return float(rel_entr(p_f, p_r).sum())


def test_rolling_ptsr_matches_the_kl_divergence_of_pattern_counts():
    rng = np.random.default_rng(2)
    arr = np.cumsum(rng.normal(size=200))
    out = rolling_ptsr(arr, window=60, d=3)
    assert np.all(np.isnan(out[:59]))
    for i in (59, 100, 199):
        expected = _kl_from_counts(arr[i - 59 : i + 1], 3)
        assert not np.isnan(expected)
        assert out[i] == pytest.approx(expected)


def test_rolling_ptsr_carries_the_previous_value_when_a_pattern_is_missing():
    rng = np.random.default_rng(3)
    noisy = np.cumsum(rng.normal(size=60))
    # a strictly rising tail: windows ending there miss most patterns
    arr = np.concatenate([noisy, noisy[-1] + np.arange(1.0, 61.0)])
    out = rolling_ptsr(arr, window=40, d=3)
    last_valid = out[59]
    assert not np.isnan(last_valid)
    assert out[-1] == pytest.approx(out[-2])
    assert np.isnan(_kl_from_counts(arr[-40:], 3))
    # nothing valid before the first full window
    assert np.all(np.isnan(out[:39]))


def test_rolling_ptsr_laplace_mode_uses_the_smoothed_library_estimate():
    arr = np.arange(80.0)
    out = rolling_ptsr(arr, window=30, d=3, missing="laplace")
    assert out[-1] == pytest.approx(library.perm_ts_reversibility(arr[-30:], d=3))


def test_rolling_ptsr_rejects_unknown_missing_mode():
    with pytest.raises(ValueError, match="missing"):
        rolling_ptsr(np.arange(50.0), window=20, missing="zero")


# ---- visibility graph + RAI ---------------------------------------------------


def test_hvg_out_degrees_by_hand():
    # links 0-1, 1-2, 2-3 and 1-3 (2 < min(3, 4))
    assert hvg_out_degrees(np.array([1.0, 3.0, 2.0, 4.0])).tolist() == [1, 2, 1, 0]
    # links 0-1, 1-2, 2-3 and 0-2 (2 < min(4, 3))
    assert hvg_out_degrees(np.array([4.0, 2.0, 3.0, 1.0])).tolist() == [2, 1, 1, 0]


def test_hvg_equal_heights_block_the_view_behind_them():
    # 0-1, 1-2 neighbours; 0-2 visible (1 < 2); 2 blocks 0 from 3 (not strictly below)
    assert hvg_out_degrees(np.array([2.0, 1.0, 2.0, 3.0])).tolist() == [2, 1, 1, 0]


def test_async_index_counts_inverted_pairs():
    a = np.array([0, 1, 2, 3])
    assert async_index(a, np.array([3, 2, 1, 0])) == pytest.approx(1.0)
    assert async_index(a, np.array([0, 1, 2, 3])) == pytest.approx(0.0)
    assert async_index(a, np.array([1, 0, 2, 3])) == pytest.approx(1 / 6)


def test_relative_async_index_by_hand():
    # forward out-degrees [1,2,1,0], reverse [2,1,1,0]: both AIs are 2/6 -> RAI 0
    assert relative_async_index(np.array([1.0, 3.0, 2.0, 4.0])) == pytest.approx(0.0)


def test_rai_separates_a_sine_from_a_chaotic_map():
    n = 240
    wave = np.sin(2 * np.pi * np.arange(n) / 12)
    logistic = np.empty(n)
    logistic[0] = 0.5
    for i in range(1, n):
        logistic[i] = -1.87 * logistic[i - 1] * (1 - logistic[i - 1])
    assert relative_async_index(logistic) > relative_async_index(wave)


def test_rolling_rai_windows_and_smoothing():
    rng = np.random.default_rng(4)
    arr = np.cumsum(rng.normal(size=120))
    raw = rolling_rai(arr, window=40)
    assert np.all(np.isnan(raw[:39]))
    assert raw[80] == pytest.approx(relative_async_index(arr[41:81]))
    smooth = rolling_rai(arr, window=40, smooth_com=7)
    expected = pd.Series(raw).ewm(com=7).mean().to_numpy()
    np.testing.assert_allclose(smooth, expected)


# ---- runs z -------------------------------------------------------------------


def test_rolling_runs_z_by_hand():
    close = pd.Series([1.0, 2.0, 1.0, 2.0, 1.0, 2.0, 1.0])
    out = rolling_runs_z(close, lookback=6)
    # 6 alternating signs: 3 up, 3 down -> mean 4, var 1.2, runs 6
    assert np.all(np.isnan(out[:6]))
    assert out[6] == pytest.approx(2.0 / math.sqrt(1.2))


def test_rolling_runs_z_is_causal():
    rng = np.random.default_rng(5)
    close = pd.Series(100 + np.cumsum(rng.normal(size=100)))
    full = rolling_runs_z(close, lookback=20)
    head = rolling_runs_z(close.iloc[:60], lookback=20)
    np.testing.assert_allclose(full[:60], head)
