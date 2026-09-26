"""Price deviation from the DeFi-TVL-implied price, traded as mean reversion.

Source: neurotrader888/TVLIndicator, ``tvl_indicator.py`` (MIT License, (c)
neurotrader888). Stonks' own implementation; no code is copied.

Indicator (per daily bar of ``ticker``, default ETH-USD.CC):

1. Take the TVL of ``chain`` (default ethereum) from the ``defi_tvl`` lake
   table, aligned causally (see below).
2. Fit ``log(close) = a + b * log(tvl)`` by OLS over the trailing
   ``fit_length`` bars *including the current bar*.
3. ``pred = exp(a + b * log(tvl_now))``, the TVL-implied price.
4. ``ind = (close - pred) / ATR``, with ATR the simple-mean true range over
   ``atr_lookback`` bars (``features.indicators.atr(method="sma")``).

TVL alignment. The vendor stamps each value with a UTC day; the value for
day ``d`` is only treated as known from the close of bar ``d + 1``. So bar
``b`` sees the latest observation with ``observation_date <= b - 1``,
carried forward over missing days. The original shifts TVL by one row so
the value lands on the bar that closes at the stamp's midnight, which
treats it as concurrent with that close; Stonks lags it one more day to be
safe against same-day revisions of the latest point. When the value in use
is more than ``max_tvl_age_days`` old (default 3; a fresh one is 1 day
old) the TVL counts as missing: no estimate on that bar, and it breaks the
regression window.

Rule (the original has none; this one is Stonks'): long when
``ind < -threshold`` (price well below its TVL-implied level), flat when
``ind > 0`` (back above it), otherwise hold. The sign was checked against
the original script's next-day-return tables on its bundled ETHUSDT daily
bars (2018-2023) with DefiLlama's ethereum series: Spearman(ind, next-day
return) is negative in every year and alignment, and ``ind < -0.25`` bars
were followed by higher mean returns than ``ind > 0.25`` bars (see
``docs/strategies/nt888-tvl.md``).

Implementation notes:

- The hysteresis state is replayed over the trailing
  ``3 * (fit_length + atr_lookback)`` bars on every call, so it starts flat
  at the start of that window; values are a pure function of data up to
  ``as_of``.
- When the TVL is constant across the fit window the slope is undefined;
  the fit then degrades to the window mean of ``log(close)``.
- ``estimate_return`` while long is ``-ind`` (the deviation in ATRs),
  floored at a tiny positive value. ``None`` when TVL is missing or stale.
- Only daily bars make sense (TVL is daily), so ``interval`` is fixed.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.features.indicators import atr
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs

#: Days between an observation's stamp and the first bar allowed to use it.
PUBLICATION_LAG_DAYS = 1

_EMPTY_TVL = (np.array([], dtype="datetime64[D]"), np.array([], dtype=float))


def align_tvl(
    bar_days: Sequence[date],
    obs_days: Sequence[date],
    tvl: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    """For each bar day ``b``, the latest TVL stamped ``<= b - 1 day`` and its
    age in days (``b - stamp``). Bars with no usable observation get NaN and
    age -1. ``obs_days`` must be sorted ascending."""
    bars = np.asarray(bar_days, dtype="datetime64[D]")
    obs = np.asarray(obs_days, dtype="datetime64[D]")
    values = np.asarray(tvl, dtype=float)
    cutoff = bars - np.timedelta64(PUBLICATION_LAG_DAYS, "D")
    idx = np.searchsorted(obs, cutoff, side="right") - 1
    has = idx >= 0
    aligned = np.full(len(bars), np.nan)
    age = np.full(len(bars), -1, dtype=int)
    aligned[has] = values[idx[has]]
    age[has] = (bars[has] - obs[idx[has]]).astype(int)
    return aligned, age


def tvl_deviation_states(ind: np.ndarray, threshold: float) -> np.ndarray:
    """Replay the rule: 1 (long) once ``ind < -threshold``, back to 0 once
    ``ind > 0``, otherwise keep the previous state. NaN keeps the state."""
    signal = np.zeros(len(ind), dtype=int)
    state = 0
    for i, v in enumerate(ind):
        if v < -threshold:
            state = 1
        elif v > 0:
            state = 0
        signal[i] = state
    return signal


def rolling_loglog_prediction(close: pd.Series, tvl: pd.Series, window: int) -> pd.Series:
    """``exp`` of the OLS fit of ``log(close)`` on ``log(tvl)`` over the
    trailing ``window`` bars (current bar included), evaluated at the
    current bar's TVL. NaN when the window holds any missing value."""
    y = np.log(close.where(close > 0))
    x = np.log(tvl.where(tvl > 0))
    mean_x = x.rolling(window).mean()
    mean_y = y.rolling(window).mean()
    var_x = x.rolling(window).var(ddof=0)
    cov = (x * y).rolling(window).mean() - mean_x * mean_y
    flat = var_x <= 1e-18
    slope = (cov / var_x.where(~flat)).where(~flat, 0.0)
    fitted = mean_y + slope * (x - mean_x)
    return np.exp(fitted)


class TVLDeviationStrategy(SingleTickerLongFlat):
    id = "tvl_deviation"
    applicable_asset_classes = ("crypto",)

    @classmethod
    def parameter_spec(cls):
        common = []
        for spec in common_specs("ETH-USD.CC"):
            if spec.name == "interval":
                spec = ParameterSpec(
                    name="interval",
                    kind="categorical",
                    default="1d",
                    bounds=["1d"],
                    tunable=False,
                    description="Bar interval. TVL is a daily series, so only daily bars.",
                )
            common.append(spec)
        return [
            ParameterSpec(
                name="fit_length",
                kind="int",
                default=7,
                bounds=(5, 60),
                description="Bars in the rolling log-log fit of close on TVL.",
            ),
            ParameterSpec(
                name="atr_lookback",
                kind="int",
                default=30,
                bounds=(10, 90),
                description="Bars in the simple-mean ATR that normalises the deviation.",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=0.25,
                bounds=(0.0, 1.0),
                description="Go long when the deviation falls below -threshold ATRs.",
            ),
            ParameterSpec(
                name="chain",
                kind="categorical",
                default="ethereum",
                bounds=None,
                tunable=False,
                description="Chain whose DeFi TVL (canonical lower-case name) drives the fit.",
            ),
            ParameterSpec(
                name="max_tvl_age_days",
                kind="int",
                default=3,
                bounds=(1, 30),
                tunable=False,
                description="Oldest usable TVL value, in days before the bar (fresh = 1).",
            ),
            *common,
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._tvl: weakref.WeakKeyDictionary[Any, tuple[np.ndarray, np.ndarray]] = (
            weakref.WeakKeyDictionary()
        )

    # ---- data ---------------------------------------------------------------

    def _tvl_series(self, lake: Any) -> tuple[np.ndarray, np.ndarray]:
        """``(observation days, tvl)`` for ``chain``, oldest first, positive
        values only; cached per lake (like :class:`LakeBarCaches`, rows
        written after the first read are not seen). A lake that can't be
        weakly referenced is re-read on every call."""
        try:
            series = self._tvl.get(lake)
            if series is None:
                series = self._load_tvl(lake)
                self._tvl[lake] = series
        except TypeError:
            return self._load_tvl(lake)
        return series

    def _load_tvl(self, lake: Any) -> tuple[np.ndarray, np.ndarray]:
        """Lakes without the TVL surface (sample lakes, stubs) yield an
        empty series."""
        reader = getattr(lake, "get_defi_tvl", None)
        if reader is None:
            return _EMPTY_TVL
        df = reader(self.params["chain"])
        df = df[pd.to_numeric(df["tvl_usd"], errors="coerce") > 0]
        if df.empty:
            return _EMPTY_TVL
        return (
            np.asarray(df["observation_date"].tolist(), dtype="datetime64[D]"),
            df["tvl_usd"].to_numpy(dtype=float),
        )

    # ---- Strategy Protocol hooks ---------------------------------------------

    def _state(self, ticker: str, as_of: Any, lake: Any) -> dict[str, float] | None:
        if lake is None:
            return None
        return self._compute(self._bar_caches.for_lake(lake), self._tvl_series(lake), ticker, as_of)

    def _compute(
        self,
        cache: BarCache,
        tvl_series: tuple[np.ndarray, np.ndarray],
        ticker: str,
        as_of: Any,
    ) -> dict[str, float] | None:
        obs_days, tvl_values = tvl_series
        if len(obs_days) == 0:
            return None
        fit_len = int(self.params["fit_length"])
        atr_lb = int(self.params["atr_lookback"])
        df = cache.last_n_bars(ticker, self.interval, as_of, 3 * (fit_len + atr_lb))
        if len(df) < max(fit_len, atr_lb + 1):
            return None

        bar_days = pd.to_datetime(df["timestamp"]).dt.date.tolist()
        tvl, age = align_tvl(bar_days, obs_days, tvl_values)
        max_age = int(self.params["max_tvl_age_days"])
        tvl = np.where((age >= 0) & (age <= max_age), tvl, np.nan)
        if np.isnan(tvl[-1]):
            return None

        close = df["close"].astype(float)
        pred = rolling_loglog_prediction(close, pd.Series(tvl), fit_len)
        a = atr(df["high"].astype(float), df["low"].astype(float), close, atr_lb, method="sma")
        ind = (close - pred) / a.where(a > 0)
        if pd.isna(ind.iloc[-1]):
            return None

        signal = tvl_deviation_states(ind.to_numpy(dtype=float), float(self.params["threshold"]))
        long = bool(signal[-1] == 1)
        last_ind = float(ind.iloc[-1])
        return {
            "close": float(close.iloc[-1]),
            "tvl": float(tvl[-1]),
            "tvl_age_days": float(age[-1]),
            "pred": float(pred.iloc[-1]),
            "atr": float(a.iloc[-1]),
            "ind": last_ind,
            "signal": 1.0 if long else 0.0,
            "score": -last_ind if long else 0.0,
        }
