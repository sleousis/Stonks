"""Sharpe-ratio inference: standard error, PSR, MinTRL, expected max Sharpe, DSR.

Every Sharpe ratio here is **per bar** (not annualised) unless the name says
otherwise, and ``kurt`` is the non-excess kurtosis (normal = 3).

References:
- Lo (2002), "The Statistics of Sharpe Ratios".
- Bailey & Lopez de Prado (2012), "The Sharpe Ratio Efficient Frontier"
  (PSR, MinTRL).
- Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio".
- Lopez de Prado, Lipton & Zoonekynd (2025), "How to use the Sharpe ratio":
  the variance with an AR(1) correction.

PSR, DSR and MinTRL evaluate the variance at the **observed** Sharpe with
``T - 1`` observations, the classic 2012 form (RS-08). Skew and kurtosis
then always count: a fat-tailed, negatively skewed series gets a lower PSR
and a longer MinTRL than a normal one with the same Sharpe, as P8 asks.
The 2025 paper's null form (variance at ``sr0``) drops both at ``sr0 = 0``,
so it is available only through ``psr(..., variance=...)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.stats import kurtosis as _kurtosis
from scipy.stats import norm
from scipy.stats import skew as _skew

EULER_GAMMA = 0.5772156649015329

NEffMethod = Literal["effective_rank", "avg_corr", "raw"]


def sharpe_variance(sr: float, t: float, skew: float, kurt: float, rho: float = 0.0) -> float:
    """Variance of the Sharpe estimator over ``t`` bars.

    ``(a - b*skew*sr + c*(kurt-1)/4*sr^2) / t`` with the AR(1) factors
    ``a = 1+2rho/(1-rho)``, ``b = 1+rho/(1-rho)+rho^2/(1-rho^2)`` and
    ``c = 1+2rho^2/(1-rho^2)``; with ``rho = 0`` this is Mertens / Lo's
    ``(1 - skew*sr + (kurt-1)/4*sr^2) / t``."""
    if t <= 0:
        raise ValueError(f"t must be positive, got {t}")
    if not -1.0 < rho < 1.0:
        raise ValueError(f"rho must lie in (-1, 1), got {rho}")
    a = 1 + 2 * rho / (1 - rho)
    b = 1 + rho / (1 - rho) + rho**2 / (1 - rho**2)
    c = 1 + 2 * rho**2 / (1 - rho**2)
    return (a - b * skew * sr + c * (kurt - 1) / 4 * sr**2) / t


def psr(
    sr: float,
    sr0: float,
    t: float,
    skew: float,
    kurt: float,
    rho: float = 0.0,
    *,
    variance: float | None = None,
) -> float:
    """Probabilistic Sharpe ratio: ``P(true SR > sr0)``.

    ``Phi((sr - sr0) / sqrt(V))`` where ``V`` defaults to the classic
    Bailey & Lopez de Prado (2012) ``sharpe_variance(sr, t - 1, ...)``: the
    estimate's own variance at the observed Sharpe, so skew and kurtosis
    always count (``t`` must exceed 1). Pass ``variance`` to use another
    form, e.g. the null form ``sharpe_variance(sr0, t, ...)``."""
    if variance is None:
        if t <= 1:
            raise ValueError(f"psr needs more than one bar, got t={t}")
        v = sharpe_variance(sr, t - 1, skew, kurt, rho)
    else:
        v = variance
    if v <= 0:
        raise ValueError(f"Sharpe variance must be positive, got {v}")
    return float(norm.cdf((sr - sr0) / math.sqrt(v)))


def min_trl(
    sr: float,
    sr0: float,
    skew: float,
    kurt: float,
    rho: float = 0.0,
    alpha: float = 0.05,
) -> float:
    """Minimum track record length, in bars, for ``psr(...) >= 1 - alpha``
    (Bailey & Lopez de Prado 2012):
    ``1 + sharpe_variance(sr, 1, ...) * (z_{1-alpha} / (sr - sr0))^2``, the
    variance at the observed Sharpe so skew and kurtosis count. Infinite
    when ``sr <= sr0`` (no track record is long enough)."""
    if sr <= sr0:
        return math.inf
    v1 = sharpe_variance(sr, 1, skew, kurt, rho)
    return 1 + v1 * (norm.ppf(1 - alpha) / (sr - sr0)) ** 2


def expected_max_sharpe(n: float, var_sr: float, mean: float = 0.0) -> float:
    """Expected maximum of ``n`` Sharpe estimates with cross-trial variance
    ``var_sr`` (false-strategy theorem, Bailey & Lopez de Prado 2014):
    ``mean + sqrt(var_sr)*((1-g)*Phi^-1(1-1/n) + g*Phi^-1(1-1/(n*e)))``.

    ``n`` may be fractional (an effective number of trials). The
    approximation is meant for ``n >= 2``; below that the maximum of one
    draw is its mean, so the excess is clamped at zero and ``n <= 1``
    returns ``mean``."""
    if var_sr < 0:
        raise ValueError(f"var_sr must be non-negative, got {var_sr}")
    if n <= 1:
        return float(mean)
    z = (1 - EULER_GAMMA) * norm.ppf(1 - 1 / n) + EULER_GAMMA * norm.ppf(1 - 1 / (n * math.e))
    return float(mean + math.sqrt(var_sr) * max(0.0, z))


def dsr(
    sr: float,
    n_trials: float,
    var_sr: float,
    t: float,
    skew: float,
    kurt: float,
    rho: float = 0.0,
) -> float:
    """Deflated Sharpe ratio: :func:`psr` against the Sharpe that the best of
    ``n_trials`` skill-less trials would show. Equals ``psr(sr, 0, ...)``
    when ``n_trials == 1``."""
    return psr(sr, expected_max_sharpe(n_trials, var_sr), t, skew, kurt, rho)


def sharpe_se_annual(sr: float, years: float) -> float:
    """Lo's (2002) iid standard error of an annualised Sharpe measured over
    ``years``: ``sqrt((1 + sr^2/2) / years)``."""
    if years <= 0:
        raise ValueError(f"years must be positive, got {years}")
    return math.sqrt((1 + 0.5 * sr**2) / years)


def effective_n(corr: np.ndarray) -> float:
    """Effective number of trials: ``exp(entropy)`` of the normalised
    eigenvalues of the trial correlation matrix (1 for identical trials,
    ``N`` for uncorrelated ones)."""
    c = np.asarray(corr, dtype=float)
    if c.ndim != 2 or c.shape[0] != c.shape[1] or c.shape[0] == 0:
        raise ValueError(f"corr must be a non-empty square matrix, got shape {c.shape}")
    eig = np.clip(np.linalg.eigvalsh(c), 0.0, None)
    total = eig.sum()
    if total <= 0:
        return 1.0
    p = eig[eig > 0] / total
    return float(math.exp(-(p * np.log(p)).sum()))


def effective_n_avg_corr(corr: np.ndarray) -> float:
    """Effective number of trials from the average off-diagonal correlation:
    ``rho_bar + (1 - rho_bar) * N``."""
    c = np.asarray(corr, dtype=float)
    n = c.shape[0]
    if n <= 1:
        return float(n)
    rho_bar = (c.sum() - np.trace(c)) / (n * (n - 1))
    return float(rho_bar + (1 - rho_bar) * n)


@dataclass(frozen=True)
class ReturnMoments:
    """What PSR / MinTRL need from a per-bar return series."""

    n: int
    sharpe: float  # per bar, ddof=1
    skew: float
    kurt: float  # non-excess (normal = 3)
    rho: float  # lag-1 autocorrelation


def return_moments(returns: np.ndarray) -> ReturnMoments:
    """Per-bar Sharpe (ddof=1), skew, non-excess kurtosis and lag-1
    autocorrelation of ``returns``; NaNs are dropped. A constant series has
    Sharpe 0, skew 0, kurtosis 3 and autocorrelation 0."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = int(r.size)
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    if n < 3 or sd == 0.0:
        return ReturnMoments(n=n, sharpe=0.0, skew=0.0, kurt=3.0, rho=0.0)
    d = r - r.mean()
    rho = float((d[1:] * d[:-1]).sum() / (d * d).sum())
    return ReturnMoments(
        n=n,
        sharpe=float(r.mean() / sd),
        skew=float(_skew(r)),
        kurt=float(_kurtosis(r, fisher=False)),
        rho=rho,
    )


@dataclass(frozen=True)
class DeflatedSharpeInputs:
    """The trial-set side of the DSR: how many trials, how many independent
    ones, how dispersed their Sharpes are and the resulting benchmark."""

    n_trials: int
    n_eff: float
    var_sr: float  # cross-trial variance of per-bar Sharpes (ddof=1)
    sr0: float  # expected_max_sharpe(n_eff, var_sr)


def deflated_sharpe_inputs(
    trials: np.ndarray,
    n_eff_method: NEffMethod = "effective_rank",
    n_trials: int | None = None,
) -> DeflatedSharpeInputs:
    """DSR inputs from a ``T x N`` matrix of per-bar trial returns.

    All-NaN (failed) and constant columns are dropped. Correlations use
    pairwise-complete bars. ``n_trials`` overrides the count (e.g. the
    cumulative count across prior runs of a strategy class); the effective
    count is then scaled by ``n_trials / columns``."""
    m = np.asarray(trials, dtype=float)
    if m.ndim != 2:
        raise ValueError(f"trials must be a 2-D T x N matrix, got shape {m.shape}")
    keep = [j for j in range(m.shape[1]) if _usable(m[:, j])]
    m = m[:, keep]
    k = m.shape[1]
    if k == 0:
        raise ValueError("no usable trial columns (all failed or constant)")
    sharpes = np.array([return_moments(m[:, j]).sharpe for j in range(k)])
    var_sr = float(np.var(sharpes, ddof=1)) if k > 1 else 0.0
    if n_eff_method == "raw" or k == 1:
        n_eff_cols = float(k)
    else:
        corr = _pairwise_corr(m)
        n_eff_cols = (
            effective_n(corr) if n_eff_method == "effective_rank" else (effective_n_avg_corr(corr))
        )
    total = k if n_trials is None else int(n_trials)
    n_eff = n_eff_cols * total / k
    return DeflatedSharpeInputs(
        n_trials=total,
        n_eff=float(n_eff),
        var_sr=var_sr,
        sr0=expected_max_sharpe(n_eff, var_sr),
    )


def _usable(col: np.ndarray) -> bool:
    finite = col[np.isfinite(col)]
    return finite.size > 2 and float(finite.std()) > 0


def _pairwise_corr(m: np.ndarray) -> np.ndarray:
    import pandas as pd

    corr = pd.DataFrame(m).corr(min_periods=3).to_numpy()
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    return corr
