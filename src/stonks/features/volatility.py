"""Volatility estimators: pure, causal, one sigma per bar (BL-09).

Every function takes pandas Series (one instrument) or DataFrames (one
column per instrument, estimated column-wise) of **adjusted** prices and
returns sigma in *per-bar* units, aligned to the input index. The value at
bar ``t`` uses bars ``<= t`` only; a window that isn't full yet is NaN.
Scale to a year with :func:`annualize` and :func:`periods_per_year`.

Estimators (all on log prices):

- :func:`close_to_close` — sample std of ``ln(C_t/C_{t-1})``.
- :func:`ewma_vol` / :func:`riskmetrics_vol` — zero-mean exponentially
  weighted sigma, ``s2_t = lam*s2_{t-1} + (1-lam)*r_t^2`` with
  ``lam = 1 - 2/(span+1)`` (Carver's span 35; RiskMetrics' lam 0.94).
- :func:`parkinson` — high/low range, ``mean(ln(H/L)^2) / (4 ln 2)``.
- :func:`garman_klass` — ``mean(0.5 ln(H/L)^2 - (2 ln2 - 1) ln(C/O)^2)``.
- :func:`rogers_satchell` — drift-independent,
  ``mean(ln(H/C) ln(H/O) + ln(L/C) ln(L/O))``.
- :func:`yang_zhang` — ``s_o^2 + k s_c^2 + (1-k) s_rs^2`` with the
  overnight ``ln(O_t/C_{t-1})`` and **open-to-close** ``ln(C_t/O_t)``
  variances and ``k = 0.34 / (1.34 + (n+1)/(n-1))``. For 24/7 markets
  (crypto) ``s_o`` is about 0 and YZ tends to a mix of s_c and RS.

Range estimators assume continuous trading within the bar; sparse
intra-bar sampling biases them low.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from stonks.backtest.calendar import calendar_for
from stonks.core.interval import Interval
from stonks.core.types import AssetClass

type Frame = pd.Series | pd.DataFrame

_LN2 = math.log(2.0)


def _check_window(n: int, minimum: int = 2) -> None:
    if n < minimum:
        raise ValueError(f"window n must be >= {minimum}, got {n}")


def _rolling_mean_sqrt(values: Frame, n: int) -> Frame:
    # a tiny negative mean (GK can go negative on odd bars) clips to 0
    return values.rolling(n, min_periods=n).mean().clip(lower=0.0) ** 0.5


def close_to_close(close: Frame, n: int = 20) -> Frame:
    """Rolling sample std (ddof=1) of ``n`` log close-to-close returns."""
    _check_window(n)
    returns = np.log(close).diff()
    return returns.rolling(n, min_periods=n).std(ddof=1)


def riskmetrics_vol(returns: Frame, lam: float = 0.94, min_periods: int = 0) -> Frame:
    """Zero-mean EWMA sigma of ``returns``: ``s2_t = lam*s2_{t-1} + (1-lam)*r_t^2``,
    seeded with the first squared return."""
    if not 0.0 < lam < 1.0:
        raise ValueError(f"lam must be in (0, 1), got {lam}")
    variance = (returns**2).ewm(alpha=1.0 - lam, adjust=False, min_periods=min_periods).mean()
    return variance**0.5


def ewma_vol(returns: Frame, span: int = 35, min_periods: int | None = None) -> Frame:
    """:func:`riskmetrics_vol` parameterised by span, ``lam = 1 - 2/(span+1)``.
    ``min_periods`` defaults to ``span`` so the warm-up bars are NaN."""
    if span < 1:
        raise ValueError(f"span must be >= 1, got {span}")
    lam = 1.0 - 2.0 / (span + 1.0)
    if span == 1:  # lam 0: sigma is |r_t|
        return returns.abs().where(returns.notna())
    return riskmetrics_vol(
        returns, lam=lam, min_periods=span if min_periods is None else min_periods
    )


def parkinson(high: Frame, low: Frame, n: int = 20) -> Frame:
    """Parkinson (1980) high-low range estimator over ``n`` bars."""
    _check_window(n, minimum=1)
    hl2 = np.log(high / low) ** 2
    return _rolling_mean_sqrt(hl2 / (4.0 * _LN2), n)


def garman_klass(open_: Frame, high: Frame, low: Frame, close: Frame, n: int = 20) -> Frame:
    """Garman-Klass (1980) OHLC estimator over ``n`` bars."""
    _check_window(n, minimum=1)
    term = 0.5 * np.log(high / low) ** 2 - (2.0 * _LN2 - 1.0) * np.log(close / open_) ** 2
    return _rolling_mean_sqrt(term, n)


def _rs_term(open_: Frame, high: Frame, low: Frame, close: Frame) -> Frame:
    return np.log(high / close) * np.log(high / open_) + np.log(low / close) * np.log(low / open_)


def rogers_satchell(open_: Frame, high: Frame, low: Frame, close: Frame, n: int = 20) -> Frame:
    """Rogers-Satchell (1991) drift-independent estimator over ``n`` bars."""
    _check_window(n, minimum=1)
    return _rolling_mean_sqrt(_rs_term(open_, high, low, close), n)


def yang_zhang(open_: Frame, high: Frame, low: Frame, close: Frame, n: int = 20) -> Frame:
    """Yang-Zhang (2000) estimator over ``n`` bars. Uses the open-to-close
    variance for ``s_c`` (not close-to-close). The first value needs ``n``
    overnight returns, hence ``n + 1`` bars."""
    _check_window(n)
    overnight = np.log(open_ / close.shift(1))
    open_close = np.log(close / open_).where(overnight.notna())
    rs = _rs_term(open_, high, low, close).where(overnight.notna())
    k = 0.34 / (1.34 + (n + 1.0) / (n - 1.0))
    variance = (
        overnight.rolling(n, min_periods=n).var(ddof=1)
        + k * open_close.rolling(n, min_periods=n).var(ddof=1)
        + (1.0 - k) * rs.rolling(n, min_periods=n).mean()
    )
    return variance.clip(lower=0.0) ** 0.5


def floor_vol(sigma: Frame, pct: float = 0.05, window: int = 500) -> Frame:
    """Carver's floor: lift sigma to at least its own trailing ``pct``
    quantile over the last ``window`` bars (current bar included), so a
    freak quiet spell can't blow up a vol-scaled position."""
    if not 0.0 <= pct <= 1.0:
        raise ValueError(f"pct must be in [0, 1], got {pct}")
    _check_window(window, minimum=1)
    floor = sigma.rolling(window, min_periods=1).quantile(pct)
    return sigma.where(sigma >= floor, floor).where(sigma.notna())


def annualize(sigma, periods_per_year: float = 252.0):
    """Per-bar sigma to annual sigma: ``sigma * sqrt(periods_per_year)``.
    Accepts scalars, Series and DataFrames."""
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year}")
    return sigma * math.sqrt(periods_per_year)


def periods_per_year(asset_class: AssetClass, interval: Interval = Interval.DAY_1) -> float:
    """Bars per year for ``asset_class`` at ``interval``, from the backtest
    trading calendar (252 equity days, 365 crypto days, ...)."""
    return calendar_for(asset_class).periods_per_year(interval)
