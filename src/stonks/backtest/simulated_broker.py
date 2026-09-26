"""In-memory broker used by the lab and by the dry-run production tick.

Implements the ``Broker`` Protocol so strategies are oblivious to whether
they're running in a backtest or in paper-mode production. Key properties:

- idempotent ``place_order`` via ``order.client_id``
- transaction costs come from a ``CostModel`` (``stonks.backtest.costs``).
  Without one, ``slippage_bps`` (adverse, on buy and sell) and a flat
  ``fee_per_trade`` build the legacy ``FixedCostModel``. The model sees the
  ticker's asset class (``set_asset_classes``; unmapped tickers are equity)
  and the bar volume passed to ``set_prices`` (``None`` when not given)
- buys larger than available cash (after costs) are scaled down to the
  largest affordable quantity; only an affordable quantity <= 0 is rejected
- fills are timestamped with the simulated ``as_of`` passed to
  ``set_prices`` (plain dates become UTC midnight, naive datetimes are
  treated as UTC); wall-clock ``now`` is used only when no ``as_of`` is set
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

from stonks.backtest.costs import CostModel, FixedCostModel, Trade, TradeCost
from stonks.core.types import AssetClass, Fill, Order, Portfolio
from stonks.logging import get_logger

_log = get_logger("stonks.backtest.simulated_broker")

# Bisection steps when scaling a buy under a non-linear cost model; 2**-50
# of the requested quantity is far below any meaningful share fraction.
_SCALE_ITERATIONS = 50


class SimulatedBroker:
    def __init__(
        self,
        portfolio: Portfolio,
        slippage_bps: float = 0.0,
        fee_per_trade: float = 0.0,
        cost_model: CostModel | None = None,
    ) -> None:
        if cost_model is not None and (slippage_bps or fee_per_trade):
            raise ValueError("pass either cost_model or slippage_bps/fee_per_trade, not both")
        self._portfolio = portfolio
        self._costs: CostModel = cost_model or FixedCostModel(slippage_bps, fee_per_trade)
        self._prices: dict[str, float] = {}
        self._volumes: dict[str, float] = {}
        self._asset_classes: dict[str, AssetClass] = {}
        self._as_of: date | None = None
        self._fills_by_client_id: dict[str, Fill] = {}
        self._fills_order: list[Fill] = []

    # ---- market data --------------------------------------------------------

    def set_prices(
        self,
        prices: Mapping[str, float],
        as_of: date,
        volumes: Mapping[str, float] | None = None,
    ) -> None:
        """Prices orders fill at, and (optionally) the volume of the bar
        they fill in. Each call replaces both; omitted volumes are unknown."""
        self._prices = dict(prices)
        self._volumes = dict(volumes or {})
        self._as_of = as_of

    def set_asset_classes(self, asset_classes: Mapping[str, AssetClass]) -> None:
        """Ticker -> asset class for the cost model; unmapped tickers are equity."""
        self._asset_classes = dict(asset_classes)

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

        quantity = order.quantity
        cost = self._cost(order, price, quantity)
        if order.side == "buy":
            if quantity * cost.fill_price + cost.fee > self._portfolio.cash:
                quantity, cost = self._affordable(order, price, quantity, cost)
                if quantity <= 0:
                    _log.debug(
                        "order_rejected",
                        client_id=order.client_id,
                        reason="insufficient_cash",
                        cash=self._portfolio.cash,
                        fee=cost.fee,
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
            price=cost.fill_price,
            fee=cost.fee,
            filled_at=self._fill_time(),
            side=order.side,
        )
        self._portfolio.apply_fill(fill)
        self._fills_by_client_id[order.client_id] = fill
        self._fills_order.append(fill)
        return fill

    # ---- internals ----------------------------------------------------------

    def _cost(self, order: Order, price: float, quantity: float) -> TradeCost:
        return self._costs.cost(
            Trade(
                ticker=order.ticker,
                side=order.side,
                quantity=quantity,
                price=price,
                asset_class=self._asset_classes.get(order.ticker, "equity"),
                bar_volume=self._volumes.get(order.ticker),
            )
        )

    def _affordable(
        self, order: Order, price: float, requested: float, cost: TradeCost
    ) -> tuple[float, TradeCost]:
        """Largest buy quantity whose notional plus fee fits in cash.

        Relies on the ``CostModel`` contract (fill price and fee
        non-decreasing in quantity). The first guess re-sizes at the
        requested size's price and fee: always affordable under that
        contract, and exact for a constant price and flat fee (the legacy
        model). If the guess is not exact, bisect between it and the
        requested quantity (which is known to be unaffordable)."""
        cash = self._portfolio.cash

        def affordable(q: float, c: TradeCost) -> bool:
            return q * c.fill_price + c.fee <= cash

        lo = max(0.0, (cash - cost.fee) / cost.fill_price)
        lo_cost = self._cost(order, price, lo) if lo > 0 else cost
        if lo > 0 and lo_cost == cost:
            return lo, cost  # constant costs: the closed form is exact
        if not affordable(lo, lo_cost):
            # the model broke the monotonicity contract; search from zero
            _log.warning("cost_model_not_monotone", client_id=order.client_id)
            lo, lo_cost = 0.0, cost
        hi = requested
        for _ in range(_SCALE_ITERATIONS):
            mid = (lo + hi) / 2
            mid_cost = self._cost(order, price, mid)
            if affordable(mid, mid_cost):
                lo, lo_cost = mid, mid_cost
            else:
                hi = mid
        if lo <= 0:
            return 0.0, cost
        return lo, lo_cost

    def _fill_time(self) -> datetime:
        as_of = self._as_of
        if as_of is None:
            return datetime.now(UTC)
        if isinstance(as_of, datetime):
            return as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        return datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC)

    def reconcile(self) -> list[Fill]:
        return list(self._fills_order)
