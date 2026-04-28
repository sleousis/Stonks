"""BuyAndHold — the simplest useful reference strategy.

Rule-based. Holds a configured ticker at a configured allocation fraction of
portfolio equity. First time we have cash and don't already own the ticker,
we buy; thereafter we sit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.strategies.base import BaseStrategy


class BuyAndHold(BaseStrategy):
    id = "buy_and_hold"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="AAPL.US",
                bounds=None,
                tunable=False,
                description="Ticker to accumulate and hold.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of available cash to deploy on entry.",
            ),
        ]

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        return 1.0 if ticker == self.params["ticker"] else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        target = self.params["ticker"]
        if portfolio.positions.get(target, 0.0) > 0:
            return []
        price = prices.get(target)
        if not price or price <= 0 or portfolio.cash <= 0:
            return []
        notional = portfolio.cash * float(self.params["allocation"])
        qty = notional / price
        if qty <= 0:
            return []
        return [
            Order(
                client_id=f"{self.id}:{target}:{as_of.isoformat()}",
                ticker=target,
                side="buy",
                quantity=qty,
                order_type="market",
                strategy_id=self.id,
            )
        ]
