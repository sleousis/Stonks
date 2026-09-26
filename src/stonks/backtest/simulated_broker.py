"""In-memory broker used by the lab and by the dry-run production tick.

Implements the ``Broker`` Protocol so strategies are oblivious to whether
they're running in a backtest or in paper-mode production. Key properties:

- idempotent ``place_order`` via ``order.client_id``
- simple slippage model (``slippage_bps`` on buy/sell price)
- flat per-trade fee
- buys larger than available cash (after fee and slippage) are scaled down
  to the affordable quantity; only an affordable quantity <= 0 is rejected
- fills are timestamped with the simulated ``as_of`` passed to
  ``set_prices`` (plain dates become UTC midnight, naive datetimes are
  treated as UTC); wall-clock ``now`` is used only when no ``as_of`` is set
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

from stonks.core.types import Fill, Order, Portfolio
from stonks.logging import get_logger

_log = get_logger("stonks.backtest.simulated_broker")


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
            _log.debug("order_rejected", client_id=order.client_id, reason="no_price")
            return None

        # slippage: adverse direction for the side
        slip = price * (self._slippage_bps / 10_000.0)
        fill_price = price + slip if order.side == "buy" else price - slip

        quantity = order.quantity
        if order.side == "buy":
            cost = fill_price * quantity + self._fee
            if cost > self._portfolio.cash:
                quantity = (self._portfolio.cash - self._fee) / fill_price
                if quantity <= 0:
                    _log.debug(
                        "order_rejected",
                        client_id=order.client_id,
                        reason="insufficient_cash",
                        cash=self._portfolio.cash,
                        fee=self._fee,
                    )
                    return None
                _log.debug(
                    "buy_scaled_to_cash",
                    client_id=order.client_id,
                    requested=order.quantity,
                    filled=quantity,
                )
        else:  # sell
            held = self._portfolio.positions.get(order.ticker, 0.0)
            if held < order.quantity:
                _log.debug(
                    "order_rejected",
                    client_id=order.client_id,
                    reason="insufficient_position",
                    held=held,
                    requested=order.quantity,
                )
                return None

        fill = Fill(
            order_client_id=order.client_id,
            ticker=order.ticker,
            quantity=quantity,
            price=fill_price,
            fee=self._fee,
            filled_at=self._fill_time(),
            side=order.side,
        )
        self._portfolio.apply_fill(fill)
        self._fills_by_client_id[order.client_id] = fill
        self._fills_order.append(fill)
        return fill

    def _fill_time(self) -> datetime:
        as_of = self._as_of
        if as_of is None:
            return datetime.now(UTC)
        if isinstance(as_of, datetime):
            return as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        return datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC)

    def reconcile(self) -> list[Fill]:
        return list(self._fills_order)
