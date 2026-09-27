"""stonks.stats.sharpe against Bailey & Lopez de Prado's published examples."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import norm

from stonks.stats.sharpe import (
    deflated_sharpe_inputs,
    dsr,
    effective_n,
    effective_n_avg_corr,
    expected_max_sharpe,
    min_trl,
    psr,
    return_moments,
    sharpe_se_annual,
    sharpe_variance,
)

EULER_GAMMA = 0.5772156649


# ---- sharpe_variance ---------------------------------------------------------


def test_sharpe_variance_under_normal_iid_is_lo_2002():
    # Normal returns: skew 0, kurt 3 -> (1 + SR^2/2) / T (Lo 2002).
    assert sharpe_variance(0.5, 100, 0.0, 3.0) == pytest.approx((1 + 0.5 * 0.25) / 100)


def test_sharpe_variance_with_zero_sharpe_is_one_over_t():
    assert sharpe_variance(0.0, 250, -3.0, 12.0) == pytest.approx(1 / 250)


def test_sharpe_variance_grows_with_negative_skew_and_fat_tails():
    base = sharpe_variance(0.2, 100, 0.0, 3.0)
    assert sharpe_variance(0.2, 100, -1.0, 3.0) > base
    assert sharpe_variance(0.2, 100, 0.0, 9.0) > base


def test_sharpe_variance_ar1_coefficients():
    rho, sr, skew, kurt, t = 0.3, 0.4, -0.5, 5.0, 60
    a = 1 + 2 * rho / (1 - rho)
    b = 1 + rho / (1 - rho) + rho**2 / (1 - rho**2)
    c = 1 + 2 * rho**2 / (1 - rho**2)
    expected = (a - b * skew * sr + c * (kurt - 1) / 4 * sr**2) / t
    assert sharpe_variance(sr, t, skew, kurt, rho) == pytest.approx(expected)


def test_positive_autocorrelation_inflates_the_variance():
    assert sharpe_variance(0.3, 100, 0, 3, rho=0.2) > sharpe_variance(0.3, 100, 0, 3)


def test_sharpe_variance_rejects_bad_inputs():
    with pytest.raises(ValueError):
        sharpe_variance(0.1, 0, 0, 3)
    with pytest.raises(ValueError):
        sharpe_variance(0.1, 10, 0, 3, rho=1.0)


# ---- psr ---------------------------------------------------------------------


def test_psr_reference_check_value_under_the_null():
    # Lopez de Prado, Lipton & Zoonekynd (2025) reference: null variance, /T.
    v = sharpe_variance(0.0, 24, -2.448, 10.164)
    assert psr(0.456, 0.0, 24, -2.448, 10.164, variance=v) == pytest.approx(0.987, abs=5e-4)


def test_psr_default_is_the_classic_form_at_the_observed_sharpe():
    # RS-08 / P8: Bailey and Lopez de Prado (2012), the estimate's own
    # variance with T - 1 observations, so skew and kurtosis count.
    v = sharpe_variance(0.456, 23, -2.448, 10.164)
    expected = norm.cdf(0.456 / math.sqrt(v))
    assert psr(0.456, 0.0, 24, -2.448, 10.164) == pytest.approx(expected)


def test_fat_tails_and_negative_skew_lower_psr_and_lengthen_min_trl():
    # RS-08: at sr0 = 0 the null form drops skew and kurtosis entirely.
    normal = psr(0.1, 0.0, 252, 0.0, 3.0)
    fat = psr(0.1, 0.0, 252, -3.0, 30.0)
    assert fat < normal
    assert min_trl(0.1, 0.0, -3.0, 30.0) > min_trl(0.1, 0.0, 0.0, 3.0)


def test_psr_needs_more_than_one_bar():
    with pytest.raises(ValueError):
        psr(0.1, 0.0, 1, 0.0, 3.0)


def test_psr_is_one_half_at_the_benchmark():
    assert psr(0.3, 0.3, 100, -1, 6) == pytest.approx(0.5)


def test_psr_rises_with_track_record_length():
    assert psr(0.1, 0, 500, 0, 3) > psr(0.1, 0, 50, 0, 3)


def test_psr_accepts_an_explicit_variance():
    # Classic Bailey & Lopez de Prado (2012) form: the estimate's own
    # variance with T - 1 observations.
    v = sharpe_variance(0.456, 23, -2.448, 10.164)
    expected = norm.cdf(0.456 / math.sqrt(v))
    assert psr(0.456, 0.0, 24, -2.448, 10.164, variance=v) == pytest.approx(expected)


# ---- min_trl -----------------------------------------------------------------


def test_min_trl_is_the_length_at_which_psr_hits_the_confidence():
    sr, sr0, skew, kurt, rho = 0.12, 0.02, -0.8, 6.0, 0.1
    t = min_trl(sr, sr0, skew, kurt, rho, alpha=0.05)
    assert psr(sr, sr0, t, skew, kurt, rho) == pytest.approx(0.95)


def test_min_trl_normal_closed_form():
    # Bailey and Lopez de Prado (2012): 1 + (1 + sr^2 / 2) * (z / sr)^2.
    z = norm.ppf(0.95)
    assert min_trl(0.1, 0.0, 0.0, 3.0) == pytest.approx(1 + (1 + 0.01 / 2) * (z / 0.1) ** 2)


def test_min_trl_shrinks_as_sharpe_grows():
    assert min_trl(0.2, 0, -1, 5) < min_trl(0.1, 0, -1, 5)


def test_min_trl_is_infinite_when_sharpe_does_not_beat_the_benchmark():
    assert math.isinf(min_trl(0.1, 0.1, 0, 3))
    assert math.isinf(min_trl(0.05, 0.1, 0, 3))


# ---- expected_max_sharpe / dsr -----------------------------------------------


def test_expected_max_sharpe_formula():
    n, v = 100, 0.5
    expected = math.sqrt(v) * (
        (1 - EULER_GAMMA) * norm.ppf(1 - 1 / n) + EULER_GAMMA * norm.ppf(1 - 1 / (n * math.e))
    )
    assert expected_max_sharpe(n, v) == pytest.approx(expected)


def test_expected_max_sharpe_bailey_lopez_de_prado_2014_example():
    # N = 100 trials with an annualised Sharpe variance of 0.5: E[max SR]
    # is about 1.7894 annualised (Bailey & Lopez de Prado 2014, section 4).
    assert expected_max_sharpe(100, 0.5) == pytest.approx(1.7894, abs=5e-4)


def test_expected_max_sharpe_grows_with_n():
    values = [expected_max_sharpe(n, 0.01) for n in (2, 5, 10, 100, 1000)]
    assert values == sorted(values)
    assert values[0] > 0


def test_expected_max_sharpe_is_zero_for_a_single_trial():
    assert expected_max_sharpe(1, 0.3) == 0.0


def test_expected_max_sharpe_adds_the_mean():
    assert expected_max_sharpe(10, 0.01, mean=0.05) == pytest.approx(
        0.05 + expected_max_sharpe(10, 0.01)
    )


def test_expected_max_sharpe_approximates_the_simulated_maximum():
    rng = np.random.default_rng(7)
    n, sd = 50, 0.1
    sim = rng.normal(0, sd, size=(20000, n)).max(axis=1).mean()
    assert expected_max_sharpe(n, sd**2) == pytest.approx(sim, rel=0.03)


def test_dsr_equals_psr_zero_with_one_trial():
    assert dsr(0.2, 1, 0.01, 250, -1, 5) == pytest.approx(psr(0.2, 0, 250, -1, 5))


def test_dsr_falls_as_trials_grow():
    assert dsr(0.15, 100, 0.002, 500, 0, 3) < dsr(0.15, 10, 0.002, 500, 0, 3)


def test_dsr_bailey_lopez_de_prado_2014_worked_example():
    # SR 2.5 annualised over 1250 daily bars, skew -3, kurt 10, N = 100,
    # V = 0.5 annualised -> DSR ~ 0.9004 with the classic PSR (estimate's
    # variance, T - 1).
    ppy = 250
    sr = 2.5 / math.sqrt(ppy)
    sr0 = expected_max_sharpe(100, 0.5 / ppy)
    assert sr0 * math.sqrt(ppy) == pytest.approx(1.7894, abs=5e-4)
    v = sharpe_variance(sr, 1250 - 1, -3.0, 10.0)
    assert psr(sr, sr0, 1250, -3.0, 10.0, variance=v) == pytest.approx(0.9004, abs=1e-3)


# ---- sharpe_se_annual ----------------------------------------------------------


def test_sharpe_se_annual():
    assert sharpe_se_annual(1.0, 4.0) == pytest.approx(math.sqrt(1.5 / 4))
    assert sharpe_se_annual(0.0, 1.0) == pytest.approx(1.0)


# ---- effective number of trials ------------------------------------------------


def test_effective_n_of_uncorrelated_trials_is_n():
    assert effective_n(np.eye(7)) == pytest.approx(7)


def test_effective_n_of_identical_trials_is_one():
    assert effective_n(np.ones((5, 5))) == pytest.approx(1)


def test_effective_n_of_two_blocks():
    corr = np.kron(np.eye(2), np.ones((3, 3)))
    assert effective_n(corr) == pytest.approx(2)


def test_effective_n_avg_corr():
    corr = np.full((4, 4), 0.5)
    np.fill_diagonal(corr, 1.0)
    assert effective_n_avg_corr(corr) == pytest.approx(0.5 + 0.5 * 4)
    assert effective_n_avg_corr(np.eye(4)) == pytest.approx(4)


# ---- moments and DSR inputs ----------------------------------------------------


def test_return_moments_on_a_known_series():
    rng = np.random.default_rng(0)
    r = rng.normal(0.001, 0.01, 200_000)
    m = return_moments(r)
    assert m.n == 200_000
    assert m.sharpe == pytest.approx(0.1, abs=0.01)
    assert m.skew == pytest.approx(0.0, abs=0.03)
    assert m.kurt == pytest.approx(3.0, abs=0.05)  # non-excess
    assert m.rho == pytest.approx(0.0, abs=0.01)


def test_return_moments_drops_nans_and_handles_constant_series():
    m = return_moments(np.array([0.01, np.nan, 0.01, 0.01]))
    assert m.n == 3
    assert m.sharpe == 0.0


def test_deflated_sharpe_inputs_from_a_trial_matrix():
    rng = np.random.default_rng(1)
    common = rng.normal(0, 0.01, (500, 1))
    trials = common + rng.normal(0, 0.01, (500, 6))
    trials[:, 5] = np.nan  # a failed trial is ignored
    inputs = deflated_sharpe_inputs(trials)
    assert inputs.n_trials == 5
    assert 1 < inputs.n_eff < 5
    sharpes = np.nanmean(trials[:, :5], axis=0) / np.nanstd(trials[:, :5], axis=0, ddof=1)
    assert inputs.var_sr == pytest.approx(np.var(sharpes, ddof=1))
    assert inputs.sr0 == pytest.approx(expected_max_sharpe(inputs.n_eff, inputs.var_sr))


def test_deflated_sharpe_inputs_raw_method_uses_the_trial_count():
    rng = np.random.default_rng(2)
    trials = rng.normal(0, 0.01, (300, 4))
    inputs = deflated_sharpe_inputs(trials, n_eff_method="raw")
    assert inputs.n_eff == 4


# ---- edge cases (review 18.1) ----------------------------------------------------


def test_deflation_with_far_more_trials_than_matrix_columns():
    rng = np.random.default_rng(4)
    m = rng.normal(0.0, 0.01, size=(300, 3))
    own = deflated_sharpe_inputs(m)
    many = deflated_sharpe_inputs(m, n_trials=1000)
    assert many.n_trials == 1000
    assert many.n_eff == pytest.approx(own.n_eff * 1000 / 3)
    assert math.isfinite(many.sr0) and many.sr0 > own.sr0 > 0


def test_identical_trial_columns_count_as_one_trial():
    rng = np.random.default_rng(5)
    col = rng.normal(0.0, 0.01, size=300)
    inputs = deflated_sharpe_inputs(np.column_stack([col, col, col]))
    assert inputs.n_eff == pytest.approx(1.0)
    assert inputs.var_sr == pytest.approx(0.0, abs=1e-15)
    assert inputs.sr0 == pytest.approx(0.0, abs=1e-9)
