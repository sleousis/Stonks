"""Donchian channel breakout — reference rule-based trend-following strategy.

Signal at bar ``t`` (using bars at the configured :class:`Interval`):

- ``upper`` = max of the ``lookback - 1`` closes *before* ``t``
- ``lower`` = min of the ``lookback - 1`` closes *before* ``t``
- close_t > upper → go long (+1)
- close_t < lower → close long  (-1)
- otherwise      → hold previous signal (forward-fill)

Our ``SimulatedBroker`` doesn't support shorts yet, so the -1 signal
translates to "flat". A full long-short version can be added once the
broker gains short-sell semantics without any change to this strategy.

Asset classes (BL-43): Grimes's breakout studies find that on single
stocks a close above an N-bar high is followed by no better than random
returns, while futures and crypto do trend after breakouts. So the
default universe is ``commodity``, ``crypto`` and ``bond`` and the
default ticker is ``BTC-USD.CC``. Equities are an opt-in through the
``asset_classes`` param (``{"asset_classes": ["equity"], "ticker":
"AAPL.US"}``). A param set saved before BL-43 still loads, but a saved
equity ticker needs that opt-in to keep trading.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.strategies._common import LakeBarCaches, long_only_decide
from stonks.strategies._vectorized import single_ticker_weights
from stonks.strategies.base import BaseStrategy


class DonchianBreakout(BaseStrategy):
    id = "donchian_breakout"
    title = "Channel breakout"
    summary = "Buys when the close tops the highest close of recent weeks and rides the trend."
    applicable_asset_classes = ("commodity", "crypto", "bond")
    hypothesis = (
        "A close above the highest close of the last N bars starts a trend "
        "in futures and crypto often enough that riding it pays for the "
        "false breakouts. Late buyers and trapped sellers fuel the move. "
        "Fails in ranges and, per Grimes, on single stocks."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 20
    required_history_bars = 21

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "required_history_bars": int(p["lookback"]) + 1,
        }

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback",
                kind="int",
                default=20,
                bounds=(5, 252),
                description="Channel lookback in bars. Max/min are computed over "
                "the prior (lookback - 1) closes.",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"],
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="BTC-USD.CC",
                bounds=None,
                tunable=False,
                description="Ticker the strategy trades.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash deployed on a fresh long entry.",
            ),
        ]

    # ---- Strategy Protocol -------------------------------------------------

    def extract_features(self, ticker: str, as_of, lake: Any) -> Features:
        state = self._compute_signal(ticker, as_of, lake)
        if state is None:
            return Features(values={})
        upper, lower, current_close, signal = state
        return Features(
            values={
                "upper_band": upper,
                "lower_band": lower,
                "close": current_close,
                "signal": float(signal),
            }
        )

    def estimate_return(self, ticker: str, as_of, lake: Any) -> float | None:
        if ticker != self.params["ticker"]:
            return None
        state = self._compute_signal(ticker, as_of, lake)
        if state is None:
            return None
        upper, _lower, current_close, signal = state
        if signal <= 0 or upper <= 0:
            return None
        # Magnitude proxy: how far above the channel ceiling we broke. Positive
        # on a fresh breakout, stays positive while the trade remains on.
        return max(current_close / upper - 1.0, 1e-6)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        return long_only_decide(
            self.id,
            self.params["ticker"],
            float(self.params["allocation"]),
            my_picks,
            portfolio,
            prices,
            as_of,
        )

    # ---- vectorised fast path (lab/vectorized.py) ---------------------------

    @classmethod
    def target_positions(cls, closes: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        """Long ``allocation`` in the ticker from a close above the channel
        until a close below it, for a whole table of daily closes. Exact
        against the event engine at ``allocation = 1``."""
        p = cls(dict(params)).params
        lookback = int(p["lookback"])

        def signal(column: pd.Series) -> np.ndarray:
            return breakout_signal(column.reset_index(drop=True), lookback).to_numpy()

        return single_ticker_weights(closes, p, signal)

    # ---- internals ---------------------------------------------------------

    def _compute_signal(
        self, ticker: str, as_of, lake: Any
    ) -> tuple[float, float, float, int] | None:
        if lake is None:
            return None

        interval = Interval.parse(self.params["interval"])
        lookback = int(self.params["lookback"])

        # The state only changes on a breakout, so the window grows until it
        # holds the latest one (or the whole history): the answer is then the
        # same as a replay from the first bar, whatever the window (RS-31).
        cache = self._bar_caches.for_lake(lake)
        n = lookback * 4 + 5
        while True:
            closes = cache.last_n_closes(ticker, interval, as_of, n)
            if len(closes) < lookback:
                return None
            s = pd.Series(closes)
            upper, lower, sig_series = _channel(s, lookback)
            if sig_series.notna().any() or len(closes) < n:
                break
            n *= 2
        sig_series = sig_series.ffill().fillna(0.0)

        last_upper = float(upper.iloc[-1]) if not pd.isna(upper.iloc[-1]) else float("nan")
        last_lower = float(lower.iloc[-1]) if not pd.isna(lower.iloc[-1]) else float("nan")
        return last_upper, last_lower, float(closes[-1]), int(sig_series.iloc[-1])


def _channel(closes: pd.Series, lookback: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """The channel over the prior ``lookback - 1`` closes and the raw
    breakout marks (1 above, -1 below, NaN inside)."""
    upper = pd.Series(closes.rolling(lookback - 1).max().shift(1))
    lower = pd.Series(closes.rolling(lookback - 1).min().shift(1))
    marks = pd.Series(np.full(len(closes), np.nan), index=closes.index)
    marks.loc[closes > upper] = 1.0
    marks.loc[closes < lower] = -1.0
    return upper, lower, marks


def breakout_signal(closes: pd.Series, lookback: int) -> pd.Series:
    """1 while long (after a close above the channel, until one below), else
    0, per bar of ``closes``."""
    _, _, marks = _channel(closes, lookback)
    return (marks.ffill().fillna(0.0) > 0).astype(float)
