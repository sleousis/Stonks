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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.strategies._common import LakeBarCaches, iso
from stonks.strategies.base import BaseStrategy


class DonchianBreakout(BaseStrategy):
    id = "donchian_breakout"

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
                default="AAPL.US",
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
        target = self.params["ticker"]
        price = prices.get(target)
        holding = portfolio.positions.get(target, 0.0)
        orders: list[Order] = []

        # breakout up (in my_picks): buy if flat
        if my_picks and price and price > 0 and holding <= 0 and portfolio.cash > 0:
            qty = (portfolio.cash * float(self.params["allocation"])) / price
            if qty > 0:
                orders.append(
                    Order(
                        client_id=f"{self.id}:buy:{target}:{iso(as_of)}",
                        ticker=target,
                        side="buy",
                        quantity=qty,
                        order_type="market",
                        strategy_id=self.id,
                    )
                )
            return orders

        # no breakout (picks empty): if we were holding, flatten.
        if not my_picks and holding > 0:
            orders.append(
                Order(
                    client_id=f"{self.id}:sell:{target}:{iso(as_of)}",
                    ticker=target,
                    side="sell",
                    quantity=holding,
                    order_type="market",
                    strategy_id=self.id,
                )
            )
        return orders

    # ---- internals ---------------------------------------------------------

    def _compute_signal(
        self, ticker: str, as_of, lake: Any
    ) -> tuple[float, float, float, int] | None:
        if lake is None:
            return None

        interval = Interval.parse(self.params["interval"])
        lookback = int(self.params["lookback"])

        # Extra bars beyond ``lookback`` let the forward-filled signal carry
        # a breakout that happened a while ago.
        closes = self._bar_caches.for_lake(lake).last_n_closes(
            ticker, interval, as_of, lookback * 4 + 5
        )
        if len(closes) < lookback:
            return None

        s = pd.Series(closes)
        upper = s.rolling(lookback - 1).max().shift(1)
        lower = s.rolling(lookback - 1).min().shift(1)

        sig_series = pd.Series(np.full(len(s), np.nan))
        sig_series.loc[s > upper] = 1.0
        sig_series.loc[s < lower] = -1.0
        sig_series = sig_series.ffill().fillna(0.0)

        last_upper = float(upper.iloc[-1]) if not pd.isna(upper.iloc[-1]) else float("nan")
        last_lower = float(lower.iloc[-1]) if not pd.isna(lower.iloc[-1]) else float("nan")
        return last_upper, last_lower, float(closes[-1]), int(sig_series.iloc[-1])
