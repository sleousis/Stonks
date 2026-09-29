"""Unit tests for the advanced technical-analysis / statistical helpers in
features/library.py:

- hawkes_process (decay-weighted volatility accumulator)
- ordinal_patterns + permutation_entropy (complexity measure)
- perm_ts_reversibility (time-asymmetry via KL of forward/reverse
  ordinal-pattern distributions)
- fit_trendlines_single / _high_low / trendline_breakout_signal
- runs_test_z_score (Wald-Wolfowitz independence test)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from stonks.features.library import (
    fit_trendlines_high_low,
    fit_trendlines_single,
    hawkes_process,
    ordinal_patterns,
    perm_ts_reversibility,
    permutation_entropy,
    runs_test_z_score,
    trendline_breakout_signal,
)

# ---- hawkes_process --------------------------------------------------------


def test_hawkes_process_of_zero_input_is_zero():
    s = pd.Series(np.zeros(50))
    out = hawkes_process(s, kappa=0.1)
    # first element undefined (no prior state); rest zero
    assert (out.iloc[1:] == 0.0).all()


def test_hawkes_larger_kappa_faster_decay():
    s = pd.Series([0.0] * 10 + [1.0] + [0.0] * 50)
    fast = hawkes_process(s, kappa=1.0).iloc[30]
    slow = hawkes_process(s, kappa=0.05).iloc[30]
    # After 20 bars post-spike, a large kappa decays ~e^-20, a small one ~e^-1
    assert abs(fast) < abs(slow)


def test_hawkes_rejects_non_positive_kappa():
    import pytest

    with pytest.raises(ValueError):
        hawkes_process(pd.Series([1.0, 2.0]), kappa=0.0)


# ---- ordinal_patterns ------------------------------------------------------


def test_ordinal_patterns_first_d_minus_1_are_nan():
    arr = np.array([1.0, 2.0, 3.0, 4.0])
    pats = ordinal_patterns(arr, d=3)
    assert np.isnan(pats[0])
    assert np.isnan(pats[1])
    assert not np.isnan(pats[2])


def test_ordinal_patterns_returns_values_in_0_to_factorial_d_minus_1():
    rng = np.random.default_rng(5)
    arr = rng.normal(size=200)
    pats = ordinal_patterns(arr, d=4)
    valid = pats[~np.isnan(pats)]
    assert (valid >= 0).all()
    assert (valid <= math.factorial(4) - 1).all()


def test_ordinal_patterns_monotone_series_uses_extreme_patterns():
    arr = np.arange(20.0)
    pats = ordinal_patterns(arr, d=3)
    # strictly increasing: pattern integer is constant for d >= 2
    valid = pats[~np.isnan(pats)]
    assert np.unique(valid).size == 1


# ---- permutation_entropy ---------------------------------------------------


def test_permutation_entropy_between_0_and_1():
    rng = np.random.default_rng(7)
    arr = rng.normal(size=400)
    pe = permutation_entropy(arr, d=3, lookback_mult=30)
    valid = pe[~np.isnan(pe)]
    assert (valid >= 0.0).all()
    assert (valid <= 1.0).all()


def test_permutation_entropy_monotone_much_less_than_random():
    arr_monotone = np.arange(400.0)
    arr_random = np.random.default_rng(4).normal(size=400)
    pe_mono = permutation_entropy(arr_monotone, d=3, lookback_mult=30)
    pe_rand = permutation_entropy(arr_random, d=3, lookback_mult=30)
    # Trailing values, past the warm-up
    assert np.nanmean(pe_mono[-50:]) < np.nanmean(pe_rand[-50:])


# ---- perm_ts_reversibility -------------------------------------------------


def test_reversibility_sine_less_than_logistic_map():
    """Sine is time-reversible; the chaotic logistic map is not. KL should
    be strictly larger for the logistic map even on short samples, where
    absolute values are noisy."""
    n = 1000
    wave = np.sin(2 * np.pi * (1 / 12) * np.arange(n))
    sine_kl = perm_ts_reversibility(wave, d=3)

    mu = 3.9
    x = np.empty(n)
    x[0] = 0.5
    for i in range(1, n):
        x[i] = mu * x[i - 1] * (1 - x[i - 1])
    logistic_kl = perm_ts_reversibility(x, d=3)

    assert not math.isnan(sine_kl)
    assert not math.isnan(logistic_kl)
    assert logistic_kl > sine_kl


def test_reversibility_is_finite_on_short_series():
    """With Laplace smoothing the result is always finite, never NaN, even
    when some patterns never appear."""
    n = 50
    rng = np.random.default_rng(0)
    arr = rng.normal(size=n)
    r = perm_ts_reversibility(arr, d=3)
    assert not math.isnan(r)
    assert math.isfinite(r)


# ---- trendline fitting -----------------------------------------------------


def test_fit_trendlines_single_on_linear_rising_data():
    data = np.linspace(100.0, 200.0, 60)
    support, resistance = fit_trendlines_single(data)
    # both lines should be near-identical on a perfect straight line
    assert support[0] > 0  # positive slope
    assert resistance[0] > 0
    assert abs(support[0] - resistance[0]) < 1e-4


def test_fit_trendlines_high_low_captures_high_and_low_envelopes():
    n = 60
    close = np.linspace(100.0, 120.0, n)
    high = close + 1.0
    low = close - 1.0
    support, resistance = fit_trendlines_high_low(high, low, close)
    assert resistance[0] > 0
    assert support[0] > 0


def test_trendline_breakout_signal_yields_correct_range():
    # flat then up: creates a breakout event
    closes = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 120.0, 60)])
    s_tl, r_tl, sig = trendline_breakout_signal(closes, lookback=30)
    # first `lookback` values are NaN / 0; signal should eventually go long
    assert np.isnan(s_tl[:30]).all()
    assert np.isnan(r_tl[:30]).all()
    assert (sig[30:] >= -1).all() and (sig[30:] <= 1).all()
    assert np.any(sig == 1.0)


# ---- runs_test_z_score -----------------------------------------------------


def test_runs_test_perfectly_alternating_high_positive_z():
    signs = np.array([1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1])
    z = runs_test_z_score(signs)
    # many more runs than expected ⇒ Z is very positive
    assert z > 2.0


def test_runs_test_single_streak_negative_z():
    # one long streak then another: very few runs ⇒ Z very negative
    signs = np.array([1] * 30 + [-1] * 30)
    z = runs_test_z_score(signs)
    assert z < -2.0


def test_runs_test_roughly_zero_for_random_signs():
    rng = np.random.default_rng(0)
    signs = np.where(rng.random(2000) > 0.5, 1, -1)
    z = runs_test_z_score(signs)
    assert abs(z) < 3.0


def test_runs_test_requires_at_least_two_elements():
    import pytest

    with pytest.raises(ValueError):
        runs_test_z_score(np.array([1]))


def test_runs_test_degenerate_all_same_is_nan():
    # Can't test independence with a constant sequence; returns NaN.
    z = runs_test_z_score(np.array([1, 1, 1, 1, 1]))
    assert math.isnan(z)


def test_runs_z_ignores_flat_moves():
    """A zero (a flat bar) is neither a rise nor a fall: counting it as a
    run of its own inflated the z-score of any series with flat bars
    (10 runs and z 6.5 here, against 5 runs and z 1.2 without them)."""
    s = np.array([1, 0, 1, -1, 1, 0, -1, 1, 0, 1], dtype=float)
    assert math.isclose(runs_test_z_score(s), runs_test_z_score(s[s != 0]))
