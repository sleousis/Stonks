"""Intramarket difference: trade one market on its trend relative to another.

Source idea: neurotrader888/IntramarketDifference. That repository carries
**no license**, so no code from it was read or reused: this module is a
clean-room implementation written only from the following description of
the algorithm.

Indicator: for each market, the close-minus-moving-average normalised by
volatility,

    cmma = (close - SMA(close, lookback)) / (ATR_rma(atr_lookback) * sqrt(lookback))

and ``diff = cmma(traded) - cmma(reference)`` computed on the timestamps
both markets share (inner join).

Rule (state machine over bars): go long when ``diff > threshold``; exit a
long when ``diff <= 0``. The original's symmetric short side (enter at
``diff < -threshold``, exit at ``diff >= 0``) is flat here because the
broker is long-only.

Implementation choices (Stonks):

- The bars of both markets are inner-joined on timestamp *before* any
  indicator is computed, so the SMA and ATR windows of both legs cover the
  same bars; a bar missing from either market is dropped from both.
- The state machine is replayed over the last ``4 * atr_lookback`` bars of
  the traded ticker on every call (state starts flat there), so the
  position is reconstructed causally from bars ``<= as_of`` only.
- No signal (``None``) when the reference has no bars, or when it has no
  bar at the traded ticker's latest timestamp (a stale reference would
  otherwise pair today's price with yesterday's).
- A zero ATR gives NaN, which never satisfies an entry or exit test.
- ``estimate_return`` while long is ``diff`` (> 0 while long), the size of
  the relative move, not a forecast return.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.features.indicators import atr
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs


def cmma(
    high: pd.Series, low: pd.Series, close: pd.Series, lookback: int, atr_lookback: int
) -> pd.Series:
    """Close minus its ``lookback`` SMA, in units of ``ATR * sqrt(lookback)``.
    NaN during warm-up and wherever the ATR is 0."""
    a = atr(high, low, close, atr_lookback, method="rma")
    sma = close.rolling(lookback).mean()
    return (close - sma) / (a.where(a > 0) * math.sqrt(lookback))


def long_flat_states(diff: np.ndarray, threshold: float) -> np.ndarray:
    """1 while long, 0 while flat: enter on ``diff > threshold``, exit on
    ``diff <= 0``. NaN never triggers either."""
    out = np.zeros(len(diff), dtype=int)
    state = 0
    for i, d in enumerate(diff):
        if state == 0 and d > threshold:
            state = 1
        elif state == 1 and d <= 0:
            state = 0
        out[i] = state
    return out


class IntramarketDifferenceStrategy(SingleTickerLongFlat):
    id = "intramarket_difference"
    title = "Linked market gap"
    summary = "Trades the gap when one market trends harder than a closely linked one."
    hypothesis = (
        "When one market trends harder than a closely linked one, the gap "
        "keeps widening for a while because flows reach them at different "
        "speeds. Fails when the link between the two markets breaks."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 24
    required_history_bars = 170

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "required_history_bars": max(int(p["lookback"]), int(p["atr_lookback"])) + 2,
        }

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback",
                kind="int",
                default=24,
                bounds=(6, 168),
                description="SMA length (bars) of the close-minus-MA indicator.",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=0.25,
                bounds=(0.05, 1.0),
                description="Entry level for cmma(traded) - cmma(reference).",
            ),
            ParameterSpec(
                name="atr_lookback",
                kind="int",
                default=168,
                bounds=(24, 336),
                description="ATR length (bars) that normalises the indicator.",
            ),
            ParameterSpec(
                name="reference_ticker",
                kind="categorical",
                default="BTC-USD.CC",
                bounds=None,
                tunable=False,
                description="Market the traded ticker is compared against.",
            ),
            *common_specs("ETH-USD.CC"),
        ]

    def data_tickers(self) -> tuple[str, ...]:
        return (str(self.params["reference_ticker"]),)

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        lookback = int(self.params["lookback"])
        atr_lb = int(self.params["atr_lookback"])
        traded = cache.last_n_bars(ticker, self.interval, as_of, 4 * atr_lb)
        if traded.empty:
            return None
        first_ts, last_ts = traded["timestamp"].iloc[0], traded["timestamp"].iloc[-1]
        ref = cache.bars_between(self.params["reference_ticker"], self.interval, first_ts, last_ts)
        if ref.empty:
            return None

        cols = ["timestamp", "high", "low", "close"]
        joined = traded[cols].merge(ref[cols], on="timestamp", suffixes=("_t", "_r"))
        joined = joined.sort_values("timestamp").reset_index(drop=True)
        if joined.empty or joined["timestamp"].iloc[-1] != last_ts:
            return None
        if len(joined) < max(lookback, atr_lb) + 2:
            return None

        c_t = cmma(joined["high_t"], joined["low_t"], joined["close_t"], lookback, atr_lb)
        c_r = cmma(joined["high_r"], joined["low_r"], joined["close_r"], lookback, atr_lb)
        diff = (c_t - c_r).to_numpy(dtype=float)
        if not np.isfinite(diff[-1]):
            return None
        long = long_flat_states(diff, float(self.params["threshold"]))[-1] == 1
        return {
            "cmma_traded": float(c_t.iloc[-1]),
            "cmma_reference": float(c_r.iloc[-1]),
            "diff": float(diff[-1]),
            "joined_bars": float(len(joined)),
            "close": float(joined["close_t"].iloc[-1]),
            "signal": 1.0 if long else 0.0,
            "score": float(diff[-1]) if long else 0.0,
        }
