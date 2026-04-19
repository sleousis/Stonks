"""In-memory broker used by the lab and by the dry-run production tick.

Implements the ``Broker`` Protocol so strategies are oblivious to whether
they're running in a backtest or in paper-mode production. Key properties:

- idempotent ``place_order`` via ``order.client_id``
- simple slippage model (``slippage_bps`` on buy/sell price)
- flat per-trade fee
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

from stonks.core.types import Fill, Order, Portfolio


class SimulatedBroker:
    def __init__(
        self,
        portfolio: Portfolio,
        slippage_bps: float = 0.0,
        fee_per_trade: float = 0.0,
    ) -> None:
        self._portfolio = portfolio
        self._slippage_bps = slippage_bps
        self._fee = fee_per_trade
        self._prices: dict[str, float] = {}
        self._as_of: date | None = None
        self._fills_by_client_id: dict[str, Fill] = {}
        self._fills_order: list[Fill] = []

    # ---- market data --------------------------------------------------------

    def set_prices(self, prices: Mapping[str, float], as_of: date) -> None:
        self._prices = dict(prices)
        self._as_of = as_of

    # ---- Broker protocol ----------------------------------------------------

    def fetch_portfolio(self) -> Portfolio:
        return self._portfolio

    def place_order(self, order: Order) -> Fill | None:
        # idempotency: same client_id returns the previously-recorded fill
        if order.client_id in self._fills_by_client_id:
            return self._fills_by_client_id[order.client_id]

        price = self._prices.get(order.ticker)
        if price is None or price <= 0:
            return None

        # slippage: adverse direction for the side
        slip = price * (self._slippage_bps / 10_000.0)
        fill_price = price + slip if order.side == "buy" else price - slip

        notional = fill_price * order.quantity

        if order.side == "buy":
            cost = notional + self._fee
            if cost > self._portfolio.cash:
                return None
        else:  # sell
            held = self._portfolio.positions.get(order.ticker, 0.0)
            if held < order.quantity:
                return None

        fill = Fill(
            order_client_id=order.client_id,
            ticker=order.ticker,
            quantity=order.quantity,
            price=fill_price,
            fee=self._fee,
            filled_at=datetime.now(UTC),
            side=order.side,
        )
        self._portfolio.apply_fill(fill)
        self._fills_by_client_id[order.client_id] = fill
        self._fills_order.append(fill)
        return fill

    def reconcile(self) -> list[Fill]:
        return list(self._fills_order)
