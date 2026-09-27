"""Helpers for the vectorised fast path (``target_positions``, BL-49, 22.5).

A strategy that can say its target weights for a whole table of daily
closes at once lets the lab screen, prune and sweep many parameter sets
cheaply (``lab/vectorized.py``). These helpers keep the strategies' own
``target_positions`` short:

- :func:`require_daily` refuses a parameter set that reads bars other than
  daily ones (the fast path only sees daily closes);
- :func:`single_ticker_weights` turns a long/flat signal for the one
  ``ticker`` a single-ticker strategy trades into a weight table;
- :func:`forecast_weights` sizes Carver forecasts the ``vol_target`` way
  (``tau * F / 10 / sigma / N``, gross capped at 1), without the 10%
  no-trade buffer and with an IDM of 1, so it is close to the event
  engine, not exact.

Row ``t`` of every result uses rows ``<= t`` only.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from stonks.features.volatility import ewma_vol
from stonks.portfolio.signals import FORECAST_TARGET

__all__ = ["forecast_weights", "require_daily", "single_ticker_weights"]

#: Annualisation for the forecast strategies' sizing sigma on daily bars.
_DAILY_PERIODS = 252.0
#: EWMA span of the sizing sigma (``_forecast_trend.SIZING_VOL_SPAN``).
_SIZING_VOL_SPAN = 35


def require_daily(params: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` when ``params`` read bars other than daily ones."""
    interval = str(params.get("interval", "1d"))
    if interval != "1d":
        raise ValueError(f"the fast path reads daily closes only, not {interval!r} bars")


def single_ticker_weights(
    closes: pd.DataFrame,
    params: Mapping[str, Any],
    signal: Callable[[pd.Series], np.ndarray],
) -> pd.DataFrame:
    """``allocation`` in ``params["ticker"]`` on every bar where
    ``signal(closes[ticker])`` is 1, flat elsewhere. Exact against the
    event engine at ``allocation = 1`` (below 1 the engine's position
    drifts with the price, the fast path rebalances)."""
    require_daily(params)
    ticker = str(params["ticker"])
    if ticker not in closes.columns:
        raise ValueError(f"no closes for {ticker!r}")
    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    column = pd.Series(closes[ticker], dtype=float)
    on = np.nan_to_num(np.asarray(signal(column), dtype=float), nan=0.0) > 0
    weights.loc[on, ticker] = float(params.get("allocation", 1.0))
    return weights


def forecast_weights(
    closes: pd.DataFrame,
    forecast: Callable[[pd.Series], pd.Series],
    *,
    tau: float,
    allow_short: bool = False,
) -> pd.DataFrame:
    """Carver sizing of each column's forecast (see the module doc): the
    names with a forecast and a sigma on a bar share the risk budget
    equally, and gross is capped at 1."""
    forecasts = pd.DataFrame(
        {t: forecast(pd.Series(closes[t], dtype=float).dropna()) for t in closes.columns},
        index=closes.index,
    )
    if not allow_short:
        forecasts = forecasts.clip(lower=0.0).where(forecasts.notna())
    log_returns = pd.DataFrame(np.log(closes.astype(float))).diff()
    sigma = ewma_vol(log_returns, span=_SIZING_VOL_SPAN) * math.sqrt(_DAILY_PERIODS)
    sigma = sigma.where(sigma > 0)
    eligible = forecasts.notna() & sigma.notna()
    n = eligible.sum(axis=1).replace(0, np.nan)
    raw = (tau * forecasts / FORECAST_TARGET / sigma).where(eligible).div(n, axis=0)
    gross = raw.abs().sum(axis=1)
    scale = (1.0 / gross).where(gross > 1.0, 1.0)
    return raw.mul(scale, axis=0).fillna(0.0)
