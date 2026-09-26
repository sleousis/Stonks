"""Carver-style trend forecasts: pure, causal, one value per bar (BL-40).

A *forecast* is a signed conviction on Carver's scale: it averages 10 in
absolute value and is capped at +/-20 (``portfolio.signals.FORECAST_TARGET``
and ``FORECAST_CAP``). Every function takes a pandas Series of **adjusted**
prices, oldest first, and returns a Series aligned to it; the value at bar
``t`` uses bars ``<= t`` only.

- :func:`ema` — recursive exponential average, seeded at the first price.
- :func:`price_change_vol` — zero-mean EWMA std of daily price *changes*
  (Carver's sigma for EWMAC, in price units).
- :func:`ewmac_raw` — ``(EMA_fast - EMA_slow) / sigma_price``; multiply by
  :data:`EWMAC_FORECAST_SCALARS` (or a lab-estimated scalar) to get a
  forecast.
- :func:`tsmom_raw` — time-series momentum over one lookback: the sign of
  the log return, or the return over its expected std (a t-like score);
  :data:`TSMOM_SCALED_SCALAR` puts the latter on the forecast scale.
- :func:`cap_forecast` and :func:`combine_forecasts` — cap, and the
  weighted, FDM-scaled average of several rules' forecasts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal

import numpy as np
import pandas as pd

from stonks.features.volatility import ewma_vol
from stonks.portfolio.signals import FORECAST_CAP

#: Carver's forecast scalars for ``EWMAC(f, 4f)`` keyed by the fast span
#: (*Systematic Trading*, table of EWMAC scalars; *Leveraged Trading*).
EWMAC_FORECAST_SCALARS: dict[int, float] = {2: 10.6, 4: 7.5, 8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87}

#: ``10 / E|Z|`` for a standard normal ``Z``: puts the scaled TSMOM score
#: (the lookback return over its std) on the forecast scale on a random walk.
TSMOM_SCALED_SCALAR = 10.0 / math.sqrt(2.0 / math.pi)

#: EWMA span of the price-change sigma (Carver uses about 36 days).
DEFAULT_VOL_SPAN = 36

TsmomMode = Literal["sign", "scaled"]


def ema(prices: pd.Series, span: int) -> pd.Series:
    """``e_t = a p_t + (1 - a) e_{t-1}``, ``a = 2 / (span + 1)``, ``e_0 = p_0``."""
    if span < 1:
        raise ValueError(f"span must be >= 1, got {span}")
    return prices.ewm(span=span, adjust=False).mean()


def price_change_vol(prices: pd.Series, span: int = DEFAULT_VOL_SPAN) -> pd.Series:
    """Zero-mean EWMA std of ``p_t - p_{t-1}`` (price units); NaN while the
    first ``span`` changes warm up, and NaN (not 0) on a flat stretch."""
    sigma = ewma_vol(prices.diff(), span=span)
    return sigma.where(sigma > 0)


def ewmac_raw(
    prices: pd.Series,
    fast: int,
    slow: int | None = None,
    vol_span: int = DEFAULT_VOL_SPAN,
) -> pd.Series:
    """Unscaled EWMAC ``(EMA_fast - EMA_slow) / sigma_price``; ``slow``
    defaults to ``4 * fast``. Positive when the fast average is above the
    slow one (an up trend)."""
    slow = 4 * fast if slow is None else slow
    if not 1 <= fast < slow:
        raise ValueError(f"need 1 <= fast < slow, got fast={fast}, slow={slow}")
    return (ema(prices, fast) - ema(prices, slow)) / price_change_vol(prices, vol_span)


def tsmom_raw(
    closes: pd.Series,
    lookback: int,
    mode: TsmomMode = "sign",
    vol_span: int = DEFAULT_VOL_SPAN,
) -> pd.Series:
    """Time-series momentum over ``lookback`` bars from the log return
    ``r = ln(C_t / C_{t-L})``:

    - ``"sign"``: ``sign(r)`` in ``{-1, 0, 1}``;
    - ``"scaled"``: ``r / (sigma_t sqrt(L))`` with ``sigma_t`` the zero-mean
      EWMA std of daily log returns.
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    if mode not in ("sign", "scaled"):
        raise ValueError(f"mode must be 'sign' or 'scaled', got {mode!r}")
    logs = np.log(closes.astype(float))
    r = logs - logs.shift(lookback)
    if mode == "sign":
        return np.sign(r)
    sigma = ewma_vol(logs.diff(), span=vol_span)
    return r / (sigma.where(sigma > 0) * math.sqrt(lookback))


def cap_forecast(forecast, cap: float = FORECAST_CAP):
    """Clip a forecast (scalar or Series) to ``[-cap, cap]``; NaN stays NaN."""
    if isinstance(forecast, pd.Series | pd.DataFrame):
        return forecast.clip(lower=-cap, upper=cap)
    return forecast if math.isnan(forecast) else max(-cap, min(cap, float(forecast)))


def combine_forecasts(
    rules: pd.DataFrame,
    weights: Mapping[str, float] | None = None,
    fdm: float = 1.0,
    cap: float = FORECAST_CAP,
) -> pd.Series:
    """Per bar, ``cap(fdm * sum_j w_j f_j / sum_j w_j)`` over the rules
    (columns) with a value on that bar; NaN where none has one. ``weights``
    default to equal. The forecast diversification multiplier ``fdm`` makes
    up for the averaging of imperfectly correlated rules (Carver)."""
    if not fdm > 0:
        raise ValueError(f"fdm must be positive, got {fdm}")
    w = pd.Series(
        {c: float(weights.get(c, 0.0)) if weights is not None else 1.0 for c in rules.columns},
        dtype=float,
    )
    present = rules.notna()
    total = present.mul(w, axis=1).sum(axis=1)
    weighted = rules.fillna(0.0).mul(w, axis=1).sum(axis=1)
    mean = (weighted / total).where(total > 0)
    return cap_forecast(fdm * mean, cap)
