"""Momentum — a technical-indicator reference strategy.

Computes the N-day trailing return and estimates expected forward return to
be that same value (a crude but standard proxy for momentum). Decide logic:
buy the top-ranked ticker with available cash; sell any existing holdings
that no longer appear in the current ranking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.strategies.base import BaseStrategy


class Momentum(BaseStrategy):
    id = "momentum"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback_days",
                kind="int",
                default=20,
                bounds=(5, 252),
                description="Lookback window for trailing return.",
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
        # pull a generous calendar window to ensure enough trading days
        start = as_of - timedelta(days=int(lookback * 1.75) + 14)
        df = lake.get_prices(ticker, start=start, end=as_of)
        if df is None or len(df) < lookback + 1:
            return None
        closes = df["close"].astype(float).tolist()
        past, now = closes[-(lookback + 1)], closes[-1]
        if past <= 0:
            return None
        return now / past - 1.0
