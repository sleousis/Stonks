"""TrendlineBreakoutStrategy — support/resistance channel breakout.

At each bar we fit a support line (to the lower envelope) and a resistance
line (to the upper envelope) to the prior ``lookback`` closes, project both
one bar forward, and emit a signal:

- ``close > projected resistance`` → go long (+1)
- ``close < projected support``    → close the long (-1, long-only broker)
- ``otherwise``                    → forward-fill the previous signal

Shape-wise this is very close to :class:`DonchianBreakout` but the
envelope is slope-aware: a trending market where highs drift up still
registers a breakout the moment price lifts through the fitted upper
line — not just a flat rolling max.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.library import trendline_breakout_signal
from stonks.strategies._common import get_last_n_bars, iso
from stonks.strategies.base import BaseStrategy


class TrendlineBreakoutStrategy(BaseStrategy):
    id = "trendline_breakout"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback",
                kind="int",
                default=72,
                bounds=(20, 300),
                description="Window size (in bars) over which support + resistance lines are fit.",
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
        support, resistance, current_close, signal = state
        return Features(
            values={
                "support": float(support),
                "resistance": float(resistance),
                "close": float(current_close),
                "signal": float(signal),
            }
        )

    def estimate_return(self, ticker: str, as_of, lake: Any) -> float | None:
        if ticker != self.params["ticker"]:
            return None
        state = self._compute_signal(ticker, as_of, lake)
        if state is None:
            return None
        _support, resistance, current_close, signal = state
        if signal <= 0 or resistance <= 0:
            return None
        return max(current_close / resistance - 1.0, 1e-6)

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

        # Need at least lookback + 1 bars; the extra tail lets the
        # forward-filled signal carry an older breakout.
        df = get_last_n_bars(lake, ticker, interval, as_of, lookback * 3 + 10)
        if df.empty or len(df) < lookback + 1:
            return None

        closes = df["close"].astype(float).to_numpy()
        s_tl, r_tl, sig = trendline_breakout_signal(closes, lookback=lookback)

        last = len(closes) - 1
        support = float(s_tl[last]) if not math.isnan(s_tl[last]) else float("nan")
        resistance = float(r_tl[last]) if not math.isnan(r_tl[last]) else float("nan")
        return support, resistance, float(closes[last]), int(sig[last])
