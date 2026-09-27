"""Fit forecast weights for one instrument, net of costs (roadmap 22.7).

The steps follow Carver (*Systematic Trading*, *Advanced Futures Trading
Strategies*) and pysystemtrade:

1. **Scale and cap.** Each rule's raw forecast times its scalar (the
   published one, or ``10 / mean|raw|`` over the training rows), capped at
   +/-20.
2. **Trade each rule on its own.** The position is ``f / 10 / sigma`` with
   ``sigma`` the EWMA std of daily returns (one unit of daily risk at an
   average forecast). The rule's net return on bar ``t`` is
   ``p_{t-1} r_t - |p_t - p_{t-1}| * cost``, with ``cost`` the one-way cost
   per unit traded as a fraction of price.
3. **Cost in Sharpe units.** Turnover is ``mean|delta f| / 10`` per bar
   times bars per year, and the yearly cost is ``turnover * cost /
   sigma_annual``. This is the Sharpe ratio the rule gives up to costs.
4. **Speed limit.** A rule whose yearly cost is above ``max_cost_sr``
   (Carver's 0.13, a third of a realistic Sharpe of 0.4, principle P19) is
   dropped for this instrument.
5. **Weights.** A :class:`ForecastWeightEstimator` weights the rules left,
   from their net returns and costs.
6. **FDM.** The forecast diversification multiplier ``1 / sqrt(w'Hw)`` from
   the correlation ``H`` of the kept rules' forecasts, capped at 2.5 (or a
   fixed value).

Everything is estimated from the rows the caller passes, which must be
training rows only (principle P12). With fewer than ``min_obs`` complete
rows the fit is a warm-up: every rule kept at equal weight, the fixed
scalars and the fallback FDM.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from stonks.features.forecast import cap_forecast
from stonks.features.forecast_weights.base import ForecastWeightEstimator, WeightInput
from stonks.features.volatility import ewma_vol
from stonks.portfolio.constructors import diversification_multiplier
from stonks.portfolio.signals import FORECAST_TARGET

#: Carver's speed limit: a rule may cost at most this Sharpe ratio a year.
DEFAULT_MAX_COST_SR = 0.13
#: Complete training rows needed before anything is estimated.
MIN_FIT_OBSERVATIONS = 250
#: EWMA span of the daily return sigma that sizes each rule's position.
POSITION_VOL_SPAN = 35

FitStatus = Literal["ok", "warmup", "too_costly"]
EstimateMode = Literal["fixed", "estimate"]


@dataclass(frozen=True)
class RuleFit:
    """One rule's part of a fit. ``cost_sr`` is the yearly cost in Sharpe
    units, ``gross_sr`` and ``net_sr`` the rule's own annualised Sharpe
    before and after costs over the training rows."""

    name: str
    scalar: float
    weight: float
    turnover: float
    cost_sr: float
    gross_sr: float
    net_sr: float
    dropped: bool
    reason: str = ""


@dataclass(frozen=True)
class ForecastWeightFit:
    """The weights, scalars and FDM one instrument trades with until the
    next fit, and why each rule is in or out."""

    method: str
    status: FitStatus
    n_obs: int
    fdm: float
    sigma_annual: float
    cost: float
    max_cost_sr: float
    rules: tuple[RuleFit, ...]
    #: Label of the last training row (a timestamp in a strategy).
    fit_end: Any = None

    @property
    def weights(self) -> dict[str, float]:
        """Weights of the kept rules."""
        return {r.name: r.weight for r in self.rules if not r.dropped}

    @property
    def scalars(self) -> dict[str, float]:
        return {r.name: r.scalar for r in self.rules}


def forecast_turnover(forecast: pd.Series, periods_per_year: float) -> float:
    """Average yearly turnover of a forecast in units of the average
    position: ``mean|delta f| / 10 * periods_per_year``."""
    changes = forecast.diff().abs().dropna()
    if changes.empty:
        return math.nan
    return float(changes.mean()) / FORECAST_TARGET * periods_per_year


def rule_net_returns(
    forecasts: pd.DataFrame, closes: pd.Series, sigma: pd.Series, cost: float
) -> pd.DataFrame:
    """Per bar, each rule's return after costs from holding
    ``f / 10 / sigma`` (see step 2 of the module doc)."""
    positions = forecasts.div(FORECAST_TARGET * sigma.where(sigma > 0), axis=0)
    returns = closes.astype(float).pct_change()
    gross = positions.shift(1).mul(returns, axis=0)
    return gross - positions.diff().abs() * cost


def _sharpe(x: pd.Series, periods_per_year: float) -> float:
    sd = float(x.std(ddof=1)) if len(x) > 1 else math.nan
    if not sd > 0:
        return math.nan
    return float(x.mean()) / sd * math.sqrt(periods_per_year)


def _scalar(raw: pd.Series, fixed: float, mode: str, min_obs: int) -> float:
    history = raw.dropna().abs()
    if mode != "estimate" or len(history) < min_obs or not float(history.mean()) > 0:
        return float(fixed)
    return FORECAST_TARGET / float(history.mean())


def fit_forecast_weights(
    raw: pd.DataFrame,
    closes: pd.Series,
    *,
    fixed_scalars: Mapping[str, float],
    cost: float,
    estimator: ForecastWeightEstimator,
    max_cost_sr: float = DEFAULT_MAX_COST_SR,
    periods_per_year: float = 252.0,
    scalar_mode: EstimateMode = "fixed",
    fdm_mode: EstimateMode = "estimate",
    fdm_fallback: float = 1.0,
    vol_span: int = POSITION_VOL_SPAN,
    min_obs: int = MIN_FIT_OBSERVATIONS,
) -> ForecastWeightFit:
    """Fit one instrument's forecast weights from training rows (module
    doc). ``raw`` holds each rule's unscaled forecast, one column per rule,
    on the same index as ``closes``."""
    if cost < 0:
        raise ValueError(f"cost must be >= 0, got {cost}")
    names = [str(c) for c in raw.columns]
    min_obs = max(2, int(min_obs))
    scalars = {n: _scalar(raw[n], fixed_scalars[n], scalar_mode, min_obs) for n in names}
    forecasts = pd.DataFrame(
        {n: cap_forecast(raw[n].astype(float) * scalars[n]) for n in names}, index=raw.index
    )
    sigma = ewma_vol(closes.astype(float).pct_change(), span=vol_span)
    net = rule_net_returns(forecasts, closes, sigma, cost)
    rows = net.dropna()
    fit_end = closes.index[-1] if len(closes) else None
    sigma_annual = (
        float(sigma.loc[rows.index].mean()) * math.sqrt(periods_per_year)
        if len(rows)
        else math.nan
    )
    gross = net + forecasts.div(FORECAST_TARGET * sigma, axis=0).diff().abs() * cost
    stats = {}
    for n in names:
        turnover = forecast_turnover(forecasts.loc[rows.index, n], periods_per_year)
        cost_sr = turnover * cost / sigma_annual if sigma_annual > 0 else math.nan
        stats[n] = (
            turnover,
            cost_sr,
            _sharpe(gross.loc[rows.index, n], periods_per_year),
            _sharpe(rows[n], periods_per_year),
        )

    def make(status: FitStatus, fdm: float, rules: tuple[RuleFit, ...]) -> ForecastWeightFit:
        return ForecastWeightFit(
            method=estimator.name,
            status=status,
            n_obs=len(rows),
            fdm=fdm,
            sigma_annual=sigma_annual,
            cost=float(cost),
            max_cost_sr=float(max_cost_sr),
            rules=rules,
            fit_end=fit_end,
        )

    if len(rows) < min_obs:
        fixed = {n: float(fixed_scalars[n]) for n in names}
        rules = tuple(
            RuleFit(n, fixed[n], 1.0 / len(names), *stats[n], dropped=False, reason="warm-up")
            for n in names
        )
        fdm = float(fdm_fallback) if len(names) > 1 else 1.0
        return make("warmup", fdm, rules)

    kept = [n for n in names if stats[n][1] <= max_cost_sr]
    if not kept:
        rules = tuple(
            RuleFit(n, scalars[n], 0.0, *stats[n], dropped=True, reason=_too_dear(stats[n][1]))
            for n in names
        )
        return make("too_costly", 1.0, rules)

    weights = estimator.estimate(
        WeightInput(rows[kept], {n: stats[n][1] for n in kept})
    )
    if len(kept) < 2:
        fdm = 1.0
    elif fdm_mode == "estimate":
        fdm = diversification_multiplier(
            forecasts.loc[rows.index, kept], weights, min_observations=min_obs
        )
    else:
        fdm = float(fdm_fallback)
    rules = tuple(
        RuleFit(
            n,
            scalars[n],
            float(weights.get(n, 0.0)) if n in kept else 0.0,
            *stats[n],
            dropped=n not in kept,
            reason="" if n in kept else _too_dear(stats[n][1]),
        )
        for n in names
    )
    return make("ok", float(fdm), rules)


def _too_dear(cost_sr: float) -> str:
    if math.isnan(cost_sr):
        return "cost unknown"
    return f"speed limit: costs {cost_sr:.3f} SR a year"


__all__ = [
    "DEFAULT_MAX_COST_SR",
    "MIN_FIT_OBSERVATIONS",
    "ForecastWeightFit",
    "RuleFit",
    "fit_forecast_weights",
    "forecast_turnover",
    "rule_net_returns",
]
