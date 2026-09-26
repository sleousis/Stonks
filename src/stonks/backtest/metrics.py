"""Pure performance metrics over an equity curve or its per-bar returns.

One home for every figure ``BacktestReport`` carries (BL-03), so reports,
survival tests and objectives read fields instead of recomputing them.

Conventions
-----------
- ``returns`` are simple per-bar returns (see :func:`bar_returns`);
  ``periods_per_year`` annualizes them (252 for daily equity bars; see
  ``stonks.backtest.calendar``).
- Drawdowns are fractions <= 0, like ``max_drawdown``. VaR and ES are
  per-bar return quantiles, so a loss is negative too.
- Ratios with a zero denominator follow the profit-factor convention: ``inf``
  (or ``-inf``) when the numerator has a sign, ``0.0`` when it is zero.
- Moments of a series that has none (fewer than two points, or no
  variance) fall back to the normal distribution's values: skew 0 and
  kurtosis 3, so the probabilistic Sharpe ratio degrades to its Gaussian
  form. Kurtosis is **not** excess kurtosis (a normal sample gives 3).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

import numpy as np

_SECONDS_PER_YEAR = 365.25 * 24 * 3600
# ``math.exp`` overflows just above 709.78.
_MAX_EXP = 709.0
# Standard-normal quantiles at 1% and 30% (Carver's lower-tail ratio).
_NORMAL_P1_OVER_P30 = 2.326 / 0.524
#: Tulchinsky's turnover floor in the fitness formula (per day).
FITNESS_TURNOVER_FLOOR = 0.125

Series = Sequence[float] | np.ndarray


def bar_returns(curve: Series) -> list[float]:
    """Simple returns between consecutive equity values, skipping steps that
    start from a non-positive value (no return is defined after a wipeout)."""
    return [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve)) if curve[i - 1] > 0]


def _ratio(num: float, den: float) -> float:
    if den != 0:
        return num / den
    if num == 0:
        return 0.0
    return math.inf if num > 0 else -math.inf


def _excess(returns: Series, periods_per_year: float, risk_free_rate: float) -> np.ndarray:
    return np.asarray(returns, dtype=float) - risk_free_rate / periods_per_year


def sharpe(returns: Series, periods_per_year: float, risk_free_rate: float = 0.0) -> float:
    """Annualized Sharpe ratio: mean excess return over its sample standard
    deviation (ddof=1). ``risk_free_rate`` is annual. 0 when there are fewer
    than two returns or no variance."""
    r = _excess(returns, periods_per_year, risk_free_rate)
    if r.size < 2:
        return 0.0
    std = float(r.std(ddof=1))
    # A constant series has a float-noise std, not a real one.
    if std <= 1e-12 * max(1.0, float(np.abs(r).max())):
        return 0.0
    return float(r.mean()) / std * math.sqrt(periods_per_year)


def sortino(returns: Series, periods_per_year: float, risk_free_rate: float = 0.0) -> float:
    """``mean(r) / sqrt(mean(min(r, 0)^2)) * sqrt(ppy)`` on excess returns;
    the downside deviation averages over every bar, not only losing ones."""
    r = _excess(returns, periods_per_year, risk_free_rate)
    if r.size == 0:
        return 0.0
    downside = math.sqrt(float(np.mean(np.minimum(r, 0.0) ** 2)))
    return _ratio(float(r.mean()), downside) * math.sqrt(periods_per_year)


def drawdowns(curve: Series) -> list[float]:
    """Per-bar drawdown from the running peak, as a fraction <= 0."""
    out: list[float] = []
    peak = -math.inf
    for v in curve:
        peak = max(peak, v)
        out.append((v - peak) / peak if peak > 0 else 0.0)
    return out


def max_drawdown(curve: Series) -> float:
    return min(drawdowns(curve), default=0.0)


def max_drawdown_duration_bars(curve: Series) -> int:
    """Longest run of consecutive bars below the running peak."""
    longest = run = 0
    for dd in drawdowns(curve):
        run = run + 1 if dd < 0 else 0
        longest = max(longest, run)
    return longest


def ulcer_index(curve: Series) -> float:
    """Root-mean-square drawdown over every bar (a fraction, not percent)."""
    dd = np.asarray(drawdowns(curve), dtype=float)
    return math.sqrt(float(np.mean(dd**2))) if dd.size else 0.0


def calmar(cagr: float, max_dd: float) -> float:
    return _ratio(cagr, abs(max_dd))


def upi(cagr: float, ulcer: float) -> float:
    """Ulcer performance index: CAGR per unit of ulcer index."""
    return _ratio(cagr, ulcer)


def value_at_risk(returns: Series, level: float = 0.95) -> float:
    """Historical per-bar VaR: the ``1 - level`` quantile of the returns."""
    r = np.asarray(returns, dtype=float)
    return float(np.quantile(r, 1.0 - level)) if r.size else 0.0


def expected_shortfall(returns: Series, level: float = 0.95) -> float:
    """Historical per-bar ES: the mean return at or below the VaR."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return 0.0
    return float(r[r <= value_at_risk(r, level)].mean())


def _central_moments(returns: Series) -> tuple[float, float, float] | None:
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return None
    d = r - r.mean()
    m2 = float(np.mean(d**2))
    if m2 <= 1e-24 * max(1.0, float(np.abs(r).max()) ** 2):
        return None
    return m2, float(np.mean(d**3)), float(np.mean(d**4))


def skew(returns: Series) -> float:
    """Moment skewness ``m3 / m2^1.5`` (0 when undefined)."""
    moments = _central_moments(returns)
    return 0.0 if moments is None else moments[1] / moments[0] ** 1.5


def kurtosis(returns: Series) -> float:
    """Moment kurtosis ``m4 / m2^2``, non-excess (normal = 3; 3 when
    undefined)."""
    moments = _central_moments(returns)
    return 3.0 if moments is None else moments[2] / moments[0] ** 2


def autocorr_1(returns: Series) -> float:
    """Lag-1 Pearson autocorrelation (0 when undefined)."""
    r = np.asarray(returns, dtype=float)
    if r.size < 3:
        return 0.0
    a, b = r[:-1], r[1:]
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def lower_tail_ratio(returns: Series) -> float:
    """Carver's lower-tail ratio: ``(p1 / p30)`` of the demeaned returns over
    the normal distribution's ``2.326 / 0.524``; 1 for a normal sample, above
    1 for a fat left tail (1 when undefined)."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return 1.0
    d = r - r.mean()
    p1, p30 = float(np.quantile(d, 0.01)), float(np.quantile(d, 0.30))
    if p1 == 0 and p30 == 0:
        return 1.0
    return _ratio(p1, p30) / _NORMAL_P1_OVER_P30


def fitness(sharpe_ratio: float, cagr: float, turnover_daily: float) -> float:
    """Tulchinsky's fitness ``sharpe * sqrt(|cagr| / max(turnover, 0.125))``
    (0 when Sharpe is 0, even with an infinite CAGR)."""
    if sharpe_ratio == 0:
        return 0.0
    return sharpe_ratio * math.sqrt(abs(cagr) / max(turnover_daily, FITNESS_TURNOVER_FLOOR))


def profit_factor(values: Series) -> float:
    """Sum of gains over the absolute sum of losses. ``inf`` with gains and no
    losses, ``0.0`` with no gains (all flat or all losses)."""
    gains = sum(v for v in values if v > 0)
    losses = abs(sum(v for v in values if v < 0))
    if gains == 0:
        return 0.0
    return math.inf if losses == 0 else gains / losses


def years_spanned(dates: Sequence[date]) -> float:
    """Wall-clock years between the first and last date (in seconds, so it
    works intraday), floored at one second."""
    floor = 1 / _SECONDS_PER_YEAR
    if len(dates) < 2:
        return floor
    span = dates[-1] - dates[0]
    return max(span.total_seconds() / _SECONDS_PER_YEAR, floor)


def cagr(start: float, end: float, years: float) -> float:
    """``(end / start) ** (1 / years) - 1``; -1 on a wipeout (``end <= 0``),
    0 for a non-positive start, and ``inf`` when the figure overflows a
    float (a tiny intraday span with any gain)."""
    if start <= 0:
        return 0.0
    if end <= 0:
        return -1.0
    exponent = math.log(end / start) / years
    if exponent > _MAX_EXP:
        return math.inf
    return math.exp(exponent) - 1.0
