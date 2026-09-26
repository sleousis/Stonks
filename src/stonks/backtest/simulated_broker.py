"""In-memory broker used by the lab and by the dry-run production tick.

Implements the ``Broker`` Protocol so strategies are oblivious to whether
they're running in a backtest or in paper-mode production. Key properties:

- idempotent ``place_order`` via ``order.client_id``
- how much of an order fills, and at what reference price, comes from a
  ``FillModel`` (``stonks.backtest.fills``). The default
  ``ImmediateFillModel`` fills every order in full at the price passed to
  ``set_prices`` (the bar open in a backtest), whatever its type;
  ``BarFillModel`` adds a participation cap, limit and stop orders against
  the bar's high / low (``set_prices(highs=, lows=)``), a zero-volume rule
  and a gap guard (``place_order(decided_at=)``). The quantity it defers is
  reported by ``unfilled_quantity(client_id)`` for the engine to re-queue.
- transaction costs come from a ``CostModel`` (``stonks.backtest.costs``).
  Without one, ``slippage_bps`` (adverse, on buy and sell) and a flat
  ``fee_per_trade`` build the legacy ``FixedCostModel``. The model sees the
  ticker's asset class (``set_asset_classes``; unmapped tickers are equity),
  the bar volume passed to ``set_prices`` (``None`` when not given) and the
  lagged ``MarketStats`` (``set_prices(stats=)``: ADV, sigma, spread)
- ``market_stats_spec`` tells the engine which lagged statistics the fill
  and cost models need (``None``: none, the legacy models)
- buys larger than the buying power (after costs) are scaled down to the
  largest affordable quantity; only an affordable quantity <= 0 is
  rejected. Buying power is cash less sale proceeds that haven't settled:
  with ``settlement_days=n`` (a cash account, T+n business days) a sale's
  proceeds are credited to cash at once (equity is unchanged) but can't pay
  for a buy until ``n`` business days after the sale. ``0`` (default)
  settles at once.
- fills are timestamped with the simulated ``as_of`` passed to
  ``set_prices`` (plain dates become UTC midnight, naive datetimes are
  treated as UTC); wall-clock ``now`` is used only when no ``as_of`` is set
- ``fills`` and ``reference_price(client_id)`` (the pre-cost price) feed the
  round-trip trade ledger (``stonks.backtest.trades``)

Long-only: a sell beyond the held quantity is rejected. Short sales
(``docs/design/shorting.md``) will add a ``sell``-to-open branch here; the
fill model already prices both sides symmetrically.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

import numpy as np

from stonks.backtest.costs import CostModel, FixedCostModel, Trade, TradeCost
from stonks.backtest.fills import (
    BarQuote,
    ExecutionSettings,
    FillModel,
    ImmediateFillModel,
    MarketStats,
    MarketStatsSpec,
)
from stonks.core.types import AssetClass, Fill, Order, Portfolio
from stonks.logging import get_logger

_log = get_logger("stonks.backtest.simulated_broker")

# Bisection steps when scaling a buy under a non-linear cost model; 2**-50
# of the requested quantity is far below any meaningful share fraction.
_SCALE_ITERATIONS = 50
_NO_STATS = MarketStats()


class SimulatedBroker:
    def __init__(
        self,
        portfolio: Portfolio,
        slippage_bps: float = 0.0,
        fee_per_trade: float = 0.0,
        cost_model: CostModel | None = None,
        fill_model: FillModel | None = None,
        settlement_days: int = 0,
    ) -> None:
        if cost_model is not None and (slippage_bps or fee_per_trade):
            raise ValueError("pass either cost_model or slippage_bps/fee_per_trade, not both")
        if settlement_days < 0:
            raise ValueError(f"settlement_days must be >= 0, got {settlement_days}")
        self._portfolio = portfolio
        self._costs: CostModel = cost_model or FixedCostModel(slippage_bps, fee_per_trade)
        self._fill_model: FillModel = fill_model or ImmediateFillModel()
        self._settlement_days = settlement_days
        self._prices: dict[str, float] = {}
        self._volumes: dict[str, float] = {}
        self._highs: dict[str, float] = {}
        self._lows: dict[str, float] = {}
        self._stats: dict[str, MarketStats] = {}
        self._asset_classes: dict[str, AssetClass] = {}
        self._as_of: date | None = None
        self._fills_by_client_id: dict[str, Fill] = {}
        self._fills_order: list[Fill] = []
        self._reference_prices: dict[str, float] = {}
        self._unfilled: dict[str, float] = {}
        #: (settlement date, proceeds) of sales not yet settled.
        self._unsettled: list[tuple[np.datetime64, float]] = []
        #: One bar's length in days (``set_bar_days``), for the gap guard.
        self._bar_days: float | None = None

    @classmethod
    def from_execution(
        cls,
        portfolio: Portfolio,
        execution: ExecutionSettings,
        cost_model: CostModel | None = None,
    ) -> SimulatedBroker:
        """A broker with ``execution``'s fill model and settlement."""
        return cls(
            portfolio,
            cost_model=cost_model,
            fill_model=execution.fill_model(),
            settlement_days=execution.settlement_days,
        )

    # ---- market data --------------------------------------------------------

    def set_prices(
        self,
        prices: Mapping[str, float],
        as_of: date,
        volumes: Mapping[str, float] | None = None,
        *,
        highs: Mapping[str, float] | None = None,
        lows: Mapping[str, float] | None = None,
        stats: Mapping[str, MarketStats] | None = None,
    ) -> None:
        """Prices orders fill at, and (optionally) the volume, high and low
        of the bar they fill in plus the lagged statistics for pricing the
        fill. Each call replaces all of them; omitted values are unknown."""
        self._prices = dict(prices)
        self._volumes = dict(volumes or {})
        self._highs = dict(highs or {})
        self._lows = dict(lows or {})
        self._stats = dict(stats or {})
        self._as_of = as_of
        self._settle(as_of)

    def set_bar_days(self, days: float | None) -> None:
        """Length of one bar in days, for the fill model's gap guard."""
        self._bar_days = days

    def set_asset_classes(self, asset_classes: Mapping[str, AssetClass]) -> None:
        """Ticker -> asset class for the cost model; unmapped tickers are equity."""
        self._asset_classes = dict(asset_classes)

    @property
    def market_stats_spec(self) -> MarketStatsSpec | None:
        """Lagged statistics the fill and cost models need, or ``None``."""
        fill_spec = getattr(self._fill_model, "market_stats_spec", None)
        cost_spec = getattr(self._costs, "market_stats_spec", None)
        if fill_spec is None:
            return cost_spec
        return fill_spec.merge(cost_spec)

    @property
    def settlement_days(self) -> int:
        return self._settlement_days

    @property
    def buying_power(self) -> float:
        """Cash less unsettled sale proceeds."""
        return self._portfolio.cash - sum(amount for _, amount in self._unsettled)

    # ---- Broker protocol ----------------------------------------------------

    def fetch_portfolio(self) -> Portfolio:
        return self._portfolio

    def place_order(self, order: Order, decided_at: datetime | None = None) -> Fill | None:
        """Fill ``order`` against the current bar. ``decided_at`` (when the
        order was decided) feeds the fill model's gap guard."""
        # idempotency: same client_id returns the previously-recorded fill
        if order.client_id in self._fills_by_client_id:
            return self._fills_by_client_id[order.client_id]

        open_ = self._prices.get(order.ticker)
        if open_ is None or open_ <= 0:
            _log.debug("order_rejected", client_id=order.client_id, reason="no_price")
            return None

        decision = self._fill_model.decide(order, self._quote(order.ticker, open_, decided_at))
        self._unfilled[order.client_id] = decision.carry
        if decision.quantity <= 0 or decision.price is None:
            _log.debug(
                "order_not_filled",
                client_id=order.client_id,
                reason=decision.reason,
                carry=decision.carry,
            )
            return None
        if decision.carry:
            _log.debug(
                "order_partially_filled",
                client_id=order.client_id,
                filled=decision.quantity,
                carry=decision.carry,
            )
        price = decision.price
        quantity = decision.quantity

        cost = self._cost(order, price, quantity)
        if order.side == "buy":
            if quantity * cost.fill_price + cost.fee > self.buying_power:
                # cash, not liquidity, binds: nothing is worth carrying
                self._unfilled[order.client_id] = 0.0
                quantity, cost = self._affordable(order, price, quantity, cost)
                if quantity <= 0:
                    _log.debug(
                        "order_rejected",
                        client_id=order.client_id,
                        reason="insufficient_cash",
                        cash=self._portfolio.cash,
                        buying_power=self.buying_power,
                        fee=cost.fee,
                    )
                    return None
                _log.debug(
                    "buy_scaled_to_cash",
                    client_id=order.client_id,
                    requested=decision.quantity,
                    filled=quantity,
                )
        else:  # sell
            held = self._portfolio.positions.get(order.ticker, 0.0)
            if held < quantity:
                self._unfilled[order.client_id] = 0.0
                _log.debug(
                    "order_rejected",
                    client_id=order.client_id,
                    reason="insufficient_position",
                    held=held,
                    requested=quantity,
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
        if order.side == "sell" and self._settlement_days:
            proceeds = quantity * cost.fill_price - cost.fee
            if proceeds > 0:
                self._unsettled.append((self._settles_on(fill.filled_at), proceeds))
        self._fills_by_client_id[order.client_id] = fill
        self._fills_order.append(fill)
        self._reference_prices[order.client_id] = price
        return fill

    def unfilled_quantity(self, client_id: str) -> float:
        """Quantity of ``client_id`` the fill model deferred to a later bar
        (participation cap, zero volume); ``0`` when it filled in full,
        expired, was cut by cash or position (a buy scaled to cash, a
        rejected sell), or was never placed."""
        return self._unfilled.get(client_id, 0.0)

    # ---- trade ledger inputs ------------------------------------------------

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Every fill, in fill order (read-only)."""
        return tuple(self._fills_order)

    def reference_price(self, client_id: str) -> float | None:
        """The pre-cost price the fill of ``client_id`` was priced from (the
        bar's open, or the limit / stop price, in a backtest), or ``None``
        when it never filled. The gap to ``Fill.price`` is the slippage and
        impact paid."""
        return self._reference_prices.get(client_id)

    # ---- internals ----------------------------------------------------------

    def _quote(self, ticker: str, open_: float, decided_at: datetime | None) -> BarQuote:
        gap = None
        if decided_at is not None and self._as_of is not None:
            gap = (self._fill_time() - _utc(decided_at)).total_seconds() / 86_400.0
        return BarQuote(
            open=open_,
            high=self._highs.get(ticker),
            low=self._lows.get(ticker),
            volume=self._volumes.get(ticker),
            adv=self._stats.get(ticker, _NO_STATS).adv,
            gap_days=gap,
            bar_days=self._bar_days,
        )

    def _cost(self, order: Order, price: float, quantity: float) -> TradeCost:
        stats = self._stats.get(order.ticker, _NO_STATS)
        return self._costs.cost(
            Trade(
                ticker=order.ticker,
                side=order.side,
                quantity=quantity,
                price=price,
                asset_class=self._asset_classes.get(order.ticker, "equity"),
                bar_volume=self._volumes.get(order.ticker),
                adv=stats.adv,
                sigma_daily=stats.sigma_daily,
                half_spread_bps=stats.half_spread_bps,
            )
        )

    def _affordable(
        self, order: Order, price: float, requested: float, cost: TradeCost
    ) -> tuple[float, TradeCost]:
        """Largest buy quantity whose notional plus fee fits in the buying
        power.

        Relies on the ``CostModel`` contract (fill price and fee
        non-decreasing in quantity). The first guess re-sizes at the
        requested size's price and fee: always affordable under that
        contract, and exact for a constant price and flat fee (the legacy
        model). If the guess is not exact, bisect between it and the
        requested quantity (which is known to be unaffordable)."""
        cash = self.buying_power

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

    def _settles_on(self, filled_at: datetime) -> np.datetime64:
        day = np.datetime64(filled_at.date(), "D")
        return np.busday_offset(day, self._settlement_days, roll="forward")

    def _settle(self, as_of: date) -> None:
        if not self._unsettled:
            return
        today = np.datetime64(as_of.date() if isinstance(as_of, datetime) else as_of, "D")
        self._unsettled = [(d, amount) for d, amount in self._unsettled if d > today]

    def _fill_time(self) -> datetime:
        as_of = self._as_of
        if as_of is None:
            return datetime.now(UTC)
        return _utc(as_of)

    def reconcile(self) -> list[Fill]:
        return list(self._fills_order)


def _utc(value: date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)
