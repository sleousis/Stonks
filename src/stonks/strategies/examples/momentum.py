"""Momentum — a technical-indicator reference strategy.

Computes the trailing return over ``lookback_days`` bars that end
``skip_days`` bars ago, and estimates expected forward return to be that
same value (a crude but standard proxy for momentum). Decide logic: buy
the top-ranked ticker with available cash; sell any existing holdings
that no longer appear in the current ranking.

Defaults (BL-43): a six-month lookback that skips the last month. Chan
finds momentum at 3 to 12 months, and one month is the short-term
reversal horizon, so Gray and Vogel skip it. The old default (20 bars, no
skip) sat right on that reversal horizon.

Old param sets: a param set that sets ``lookback_days`` but not
``skip_days`` (every set saved before BL-43) keeps its original meaning,
``skip_days=0``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.strategies._common import LakeBarCaches
from stonks.strategies.base import BaseStrategy


class Momentum(BaseStrategy):
    id = "momentum"
    hypothesis = (
        "Stocks that rose most over the last six months, skipping the last "
        "month, keep rising for a few months: investors under-react to news "
        "and then herd. Losers are the late sellers. Fails in sharp "
        "reversals such as bear-market rebounds."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 21
    required_history_bars = 148

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "required_history_bars": int(p["lookback_days"]) + int(p["skip_days"]) + 1,
        }

    def __init__(self, params: Any) -> None:
        params = dict(params)
        if "lookback_days" in params and "skip_days" not in params:
            params["skip_days"] = 0  # a pre-BL-43 param set: no skip
        super().__init__(params)
        # A backtest calls estimate_return for every ticker on every bar;
        # the per-instance, per-lake bar cache reads each ticker's history
        # once and slices it to ``as_of`` in memory.
        self._bar_caches = LakeBarCaches()

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback_days",
                kind="int",
                default=126,
                # Tuning range: 63-252 is the momentum horizon; 5 is kept
                # as the floor so param sets saved before BL-43 still load.
                bounds=(5, 252),
                description="Lookback window for trailing return, in bars.",
            ),
            ParameterSpec(
                name="skip_days",
                kind="int",
                default=21,
                bounds=(0, 63),
                # Fixed, not tuned (P4): the skip month is the literature's
                # choice, and tuning it adds a parameter to overfit.
                tunable=False,
                description="Most recent bars left out of the window (the reversal month).",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=0.0,
                bounds=(-1.0, 1.0),
                description="Minimum trailing return to become a pick.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash deployed on the top pick.",
            ),
        ]

    def extract_features(self, ticker: str, as_of: date, lake: Any) -> Features:
        r = self._lookback_return(ticker, as_of, lake)
        return Features(values={"r_lookback": r} if r is not None else {})

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        r = self._lookback_return(ticker, as_of, lake)
        if r is None:
            return None
        return r if r > self.params["threshold"] else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        orders: list[Order] = []

        picked_tickers = {t for _, t in my_picks}

        # 1. sell positions that fell out of the ranking
        for ticker, qty in list(portfolio.positions.items()):
            if qty > 0 and ticker not in picked_tickers:
                orders.append(
                    Order(
                        client_id=f"{self.id}:sell:{ticker}:{as_of.isoformat()}",
                        ticker=ticker,
                        side="sell",
                        quantity=qty,
                        order_type="market",
                        strategy_id=self.id,
                    )
                )

        # 2. buy the top pick if we don't already hold it
        if my_picks:
            sorted_picks = sorted(my_picks, key=lambda p: p[0], reverse=True)
            _, top = sorted_picks[0]
            price = prices.get(top)
            if (
                price
                and price > 0
                and portfolio.positions.get(top, 0.0) <= 0
                and portfolio.cash > 0
            ):
                qty = (portfolio.cash * float(self.params["allocation"])) / price
                if qty > 0:
                    orders.append(
                        Order(
                            client_id=f"{self.id}:buy:{top}:{as_of.isoformat()}",
                            ticker=top,
                            side="buy",
                            quantity=qty,
                            order_type="market",
                            strategy_id=self.id,
                        )
                    )

        return orders

    # ---- internals ---------------------------------------------------------

    def _lookback_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        if lake is None:
            return None
        lookback = int(self.params["lookback_days"])
        skip = int(self.params["skip_days"])
        # Only daily bars complete at this decision (RS-03): the day's own
        # bar at a daily decision, the previous session's mid-session.
        closes = self._bar_caches.for_lake(lake).last_n_closes(
            ticker, Interval.DAY_1, as_of, lookback + skip + 1
        )
        if len(closes) < lookback + skip + 1:
            return None
        past, now = closes[0], closes[-1 - skip]
        if past <= 0:
            return None
        return float(now / past - 1.0)
