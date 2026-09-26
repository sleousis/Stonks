"""Market-profile support/resistance penetration strategy.

Source: neurotrader888/TechnicalAnalysisAutomation, ``mp_support_resist.py``
(MIT License, (c) 2023 neurotrader888). Stonks' own implementation; no code
is copied. Level finding lives in
:func:`stonks.features.market_profile.market_profile_levels`.

Levels at bar ``i``: a weighted Gaussian KDE of the last ``lookback`` log
closes (weights rising linearly from ``first_w`` to 1), bandwidth factor
``ATR(log H, log L, log C, lookback) * atr_mult``, and every density peak
with prominence ``>= prom_thresh * max density``.

Rule (state machine over bars): if the close crosses up through any of bar
``i``'s levels (``close[i-1] <= level < close[i]``) go long; if it crosses
down through one (``close[i-1] >= level > close[i]``) the original goes
short, which is flat here; otherwise keep the previous state. When one bar
crosses several levels, the last level in ascending order decides (as in
the original).

Deliberate deviations from the original:

- Short -> flat (long-only broker).
- The ATR at bar ``i`` is computed over the ``2 * lookback`` bars ending at
  ``i`` (the original runs it over the whole history), so every bar's levels
  are a pure function of a fixed window. That makes them safe to cache and
  identical however the history was fetched.
- The state machine is replayed over the trailing ``lookback`` bars on every
  call (state starts flat at the replay start). Levels are cached per
  ``(lake, ticker, interval, bar timestamp)`` on the instance, so a backtest
  computes one KDE per new bar instead of ``lookback`` of them. The cache
  only ever holds values derived from bars at or before their own
  timestamp, so it cannot leak future data.
- ``estimate_return`` while long is ``close / level_crossed - 1`` (floored at
  a tiny positive value): the distance above the level that triggered.
"""

from __future__ import annotations

import weakref
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.features.indicators import atr
from stonks.features.market_profile import market_profile_levels
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs


class MarketProfileSRStrategy(SingleTickerLongFlat):
    id = "market_profile_sr"
    hypothesis = (
        "Prices where much trading happened act as support and "
        "resistance. A close up through such a level shows buyers won and "
        "price moves to the next level. Fails when levels are too close "
        "to matter."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 24

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        # keyed weakly by the per-lake BarCache so lakes never share levels
        self._levels: weakref.WeakKeyDictionary[BarCache, dict[tuple, list[float]]] = (
            weakref.WeakKeyDictionary()
        )

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback",
                kind="int",
                default=365,
                bounds=(100, 500),
                description="Closes in each market profile, also the log-ATR length "
                "and the replay length (bars).",
            ),
            ParameterSpec(
                name="first_w",
                kind="float",
                default=0.01,
                bounds=(0.01, 1.0),
                description="KDE weight of the oldest close; weights rise linearly to 1.",
            ),
            ParameterSpec(
                name="atr_mult",
                kind="float",
                default=3.0,
                bounds=(1.0, 5.0),
                description="KDE bandwidth factor = log ATR * atr_mult.",
            ),
            ParameterSpec(
                name="prom_thresh",
                kind="float",
                default=0.25,
                bounds=(0.1, 0.5),
                description="Minimum peak prominence as a fraction of the max density.",
            ),
            *common_specs("BTC-USD.CC"),
        ]

    # ---- levels -------------------------------------------------------------

    def _level_cache(self, cache: BarCache) -> dict[tuple, list[float]]:
        try:
            store = self._levels.get(cache)
            if store is None:
                store = {}
                self._levels[cache] = store
        except TypeError:
            return {}
        return store

    def _levels_for(self, window: pd.DataFrame) -> list[float]:
        """Levels for the last bar of ``window`` (its trailing 2*lookback bars)."""
        lookback = int(self.params["lookback"])
        highs = window["high"].astype(float)
        lows = window["low"].astype(float)
        closes = window["close"].astype(float)
        if (highs <= 0).any() or (lows <= 0).any() or (closes <= 0).any():
            return []
        log_c = np.log(closes)
        log_atr = atr(np.log(highs), np.log(lows), log_c, lookback, method="rma").iloc[-1]
        return market_profile_levels(
            log_c.to_numpy()[-lookback:],
            float(log_atr),
            first_w=float(self.params["first_w"]),
            atr_mult=float(self.params["atr_mult"]),
            prom_thresh=float(self.params["prom_thresh"]),
        )

    def _levels_at(
        self, store: dict[tuple, list[float]], df: pd.DataFrame, j: int, ticker: str
    ) -> list[float]:
        span = 2 * int(self.params["lookback"])
        key = (ticker, self.params["interval"], pd.Timestamp(df["timestamp"].iloc[j]))
        levels = store.get(key)
        if levels is None:
            levels = self._levels_for(df.iloc[j - span + 1 : j + 1])
            store[key] = levels
        return levels

    def support_resistance_levels(self, ticker: str, as_of: Any, lake: Any) -> list[float]:
        """Levels at the latest bar ``<= as_of`` ([] without enough history)."""
        cache = self._bar_caches.for_lake(lake)
        span = 2 * int(self.params["lookback"])
        df = cache.last_n_bars(ticker, self.interval, as_of, span)
        if len(df) < span:
            return []
        return self._levels_at(self._level_cache(cache), df, len(df) - 1, ticker)

    # ---- signal -------------------------------------------------------------

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        lookback = int(self.params["lookback"])
        span = 2 * lookback
        df = cache.last_n_bars(ticker, self.interval, as_of, span + lookback)
        if len(df) < span:
            return None
        store = self._level_cache(cache)
        closes = df["close"].to_numpy(dtype=float)
        start = max(span - 1, len(df) - 1 - lookback)

        state, entry_level = 0, float("nan")
        levels: list[float] = []
        for j in range(start, len(df)):
            levels = self._levels_at(store, df, j, ticker)
            prev_c, cur_c = closes[j - 1], closes[j]
            for level in levels:
                if prev_c <= level < cur_c:
                    state, entry_level = 1, level
                elif prev_c >= level > cur_c:
                    state, entry_level = 0, float("nan")

        close = float(closes[-1])
        below = [lv for lv in levels if lv <= close]
        above = [lv for lv in levels if lv > close]
        long = state == 1
        return {
            "close": close,
            "n_levels": float(len(levels)),
            "level_below": max(below) if below else float("nan"),
            "level_above": min(above) if above else float("nan"),
            "entry_level": entry_level,
            "signal": 1.0 if long else 0.0,
            "score": close / entry_level - 1.0 if long and entry_level > 0 else 0.0,
        }
