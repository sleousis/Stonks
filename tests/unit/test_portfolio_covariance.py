"""Covariance estimators and the PSD fix (BL-44)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.portfolio.covariance import (
    CovarianceEstimator,
    estimator_names,
    get_estimator,
    nearest_psd,
)


def _returns(n: int = 500, k: int = 4, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(scale=0.01, size=(n, k)), columns=list("ABCD")[:k])


def test_registry_lists_the_four_estimators() -> None:
    assert {"sample", "ledoit_wolf", "ewma", "denoised"} <= set(estimator_names())
    assert all(isinstance(get_estimator(n), CovarianceEstimator) for n in estimator_names())


def test_unknown_estimator_names_the_valid_ones() -> None:
    with pytest.raises(ValueError, match="ledoit_wolf"):
        get_estimator("nope")


def test_sample_matches_numpy() -> None:
    r = _returns()
    np.testing.assert_allclose(get_estimator("sample").estimate(r), np.cov(r.T, ddof=1))


def test_ledoit_wolf_shrinks_towards_scaled_identity() -> None:
    r = _returns(n=30, k=4)
    sample = np.cov(r.T, ddof=1)
    lw = get_estimator("ledoit_wolf").estimate(r)
    off = ~np.eye(4, dtype=bool)
    assert np.abs(lw[off]).sum() < np.abs(sample[off]).sum()
    assert np.allclose(lw, lw.T)


def test_ewma_zero_mean_weights_recent_rows_more() -> None:
    # one big shock long ago, one small recently: EWMA leans on the recent one
    r = pd.DataFrame({"A": [0.1] + [0.0] * 98 + [0.01]})
    cov = get_estimator("ewma", lam=0.9).estimate(r)
    w = 0.9 ** np.arange(100)[::-1]
    w /= w.sum()
    expected = w[0] * 0.1**2 + w[-1] * 0.01**2
    assert cov[0, 0] == pytest.approx(expected)


def test_ewma_rejects_bad_lambda() -> None:
    with pytest.raises(ValueError):
        get_estimator("ewma", lam=1.5)


def test_denoised_keeps_the_diagonal_and_flattens_noise() -> None:
    r = _returns(n=60, k=4)
    sample = np.cov(r.T, ddof=1)
    den = get_estimator("denoised").estimate(r)
    np.testing.assert_allclose(np.diag(den), np.diag(sample), rtol=1e-8)
    assert np.all(np.linalg.eigvalsh(den) > 0)


def test_denoised_keeps_a_strong_factor() -> None:
    rng = np.random.default_rng(3)
    factor = rng.normal(scale=0.02, size=(400, 1))
    r = pd.DataFrame(factor + rng.normal(scale=0.005, size=(400, 5)))
    den = get_estimator("denoised").estimate(r)
    corr = den / np.sqrt(np.outer(np.diag(den), np.diag(den)))
    assert corr[0, 1] > 0.8


@pytest.mark.parametrize("name", ["sample", "ledoit_wolf", "ewma", "denoised"])
def test_every_estimator_returns_psd_on_singular_input(name: str) -> None:
    r = _returns(n=10, k=3)
    r["D"] = r["A"]  # duplicate column: singular sample covariance
    cov = get_estimator(name).estimate(r)
    assert cov.shape == (4, 4)
    assert np.all(np.isfinite(cov))
    assert np.linalg.eigvalsh(cov).min() > 0


@pytest.mark.parametrize("name", ["sample", "ledoit_wolf", "ewma", "denoised"])
def test_every_estimator_needs_two_rows(name: str) -> None:
    with pytest.raises(ValueError, match="rows"):
        get_estimator(name).estimate(_returns(n=1))


def test_nearest_psd_lifts_negative_eigenvalues() -> None:
    bad = np.array([[1.0, 0.9, -0.9], [0.9, 1.0, 0.9], [-0.9, 0.9, 1.0]])
    assert np.linalg.eigvalsh(bad).min() < 0
    fixed = nearest_psd(bad)
    assert np.linalg.eigvalsh(fixed).min() > 0
    assert np.allclose(fixed, fixed.T)


def test_nearest_psd_leaves_a_good_matrix_alone() -> None:
    good = np.array([[2.0, 0.5], [0.5, 1.0]])
    np.testing.assert_allclose(nearest_psd(good), good)


def test_nearest_psd_handles_all_zero() -> None:
    fixed = nearest_psd(np.zeros((2, 2)))
    assert np.linalg.eigvalsh(fixed).min() > 0
