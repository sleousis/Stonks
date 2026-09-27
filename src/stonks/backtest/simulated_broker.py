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

Long-only by default: a sell beyond the held quantity is rejected.

Resting stops (roadmap 19.10): a ``stop`` or ``stop_limit`` order with
``time_in_force="gtc"`` does not fill when placed. It rests until
``cancel_order`` removes it or ``trigger_resting`` finds a bar (the prices,
highs and lows of the last ``set_prices``) that reaches it: a sell stop
when the low touches it, a buy stop when the high does, at the stop or at
a gapped open (``backtest.fills.triggered_price``). A resting stop only
closes: it never sells more than is held long or buys more than is short,
and one with nothing left to close is dropped.

Short selling and margin (roadmap 16.1, ``docs/design/shorting.md``)
--------------------------------------------------------------------
With a ``margin`` model other than ``cash`` (``RegTMargin``) the broker
checks opening orders against the model's excess equity instead of cash:
an opening buy or short sale larger than the room is scaled down, covers
and sells of a long are never limited by margin, and cash may go negative
(a margin loan). With ``allow_short`` (which needs a model that allows
shorts, ``enable_shorts``) a sell beyond the held quantity closes the long
and sells the rest short:

1. the ``BorrowSource`` (default ``FlatBorrow``) must quote the ticker as
   borrowable on the fill day, else the short part is dropped
   (``not_borrowable``), and ``available_shares`` caps it;
2. the margin model's initial requirement must fit the excess equity left
   after the closing part, else the short part is scaled down;
3. it fills on the sell side of the cost model and credits the proceeds.

``accrue(as_of)`` charges financing once per day: the borrow fee on every
short (``|qty| x price x fee / 360`` per calendar day, at the current
prices) and debit interest on negative cash. Each charge is a
``FinancingEvent`` in ``financing``. A long-only cash book accrues nothing.
``margin_call()`` plans the closes that cure a maintenance breach, most
losing positions first, and ``recalled(as_of)`` lists shorts whose borrow
was withdrawn (sources with history only).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Literal

import numpy as np

from stonks.backtest.calendar import calendar_for
from stonks.backtest.costs import CostModel, FixedCostModel, Trade, TradeCost
from stonks.backtest.fills import (
    BarQuote,
    ExecutionSettings,
    FillModel,
    ImmediateFillModel,
    MarketStats,
    MarketStatsSpec,
    triggered_price,
)
from stonks.core.interval import Interval
from stonks.core.types import AssetClass, Fill, Order, Portfolio
from stonks.execution.borrow import DAY_COUNT, BorrowSource, FlatBorrow, daily_fee
from stonks.execution.margin import MarginModel
from stonks.logging import get_logger

_log = get_logger("stonks.backtest.simulated_broker")

# Bisection steps when scaling a buy under a non-linear cost model; 2**-50
# of the requested quantity is far below any meaningful share fraction.
_SCALE_ITERATIONS = 50
#: Relative excess of a sell over the holding that is float dust.
_DUST = 1e-9
_NO_STATS = MarketStats()

FinancingKind = Literal["borrow_fee", "debit_interest"]


@dataclass(frozen=True)
class FinancingEvent:
    """One financing charge debited to cash by ``accrue``."""

    timestamp: datetime
    #: The short the borrow fee is for; ``None`` for debit interest.
    ticker: str | None
    kind: FinancingKind
    #: Cash change (negative: a charge).
    amount: float
    #: Calendar days the charge covers.
    days: int


class SimulatedBroker:
    def __init__(
        self,
        portfolio: Portfolio,
        slippage_bps: float = 0.0,
        fee_per_trade: float = 0.0,
        cost_model: CostModel | None = None,
        fill_model: FillModel | None = None,
        settlement_days: int = 0,
        *,
        margin: MarginModel | None = None,
        borrow: BorrowSource | None = None,
        allow_short: bool = False,
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
        #: Good till cancelled stops waiting for a bar that reaches them.
        self._resting: dict[str, Order] = {}
        #: (settlement date, proceeds) of sales not yet settled.
        self._unsettled: list[tuple[np.datetime64, float]] = []
        #: The bar interval (``set_interval``) and one bar's length in days.
        self._interval: Interval | None = None
        self._bar_days: float | None = None
        #: Margin, borrow and financing (see the module doc). ``None``
        #: margin is the cash account: the legacy code path, unchanged.
        self._margin: MarginModel | None = None
        self._borrow: BorrowSource = borrow or FlatBorrow()
        self._allow_short = False
        self._financing: list[FinancingEvent] = []
        self._accrued_through: date | None = None
        #: Average entry price per held ticker (margin-call ordering).
        self._avg_cost: dict[str, float] = {}
        if margin is not None and margin.name != "cash":
            self._margin = margin
        if allow_short:
            self.enable_shorts(margin, borrow)

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

    def set_interval(self, interval: Interval | None) -> None:
        """The backtest's bar interval: the fill model's gap guard reads one
        bar's length, and the cost model annualises volatility with the bars
        per year of the interval on each asset class's calendar (RS-19)."""
        self._interval = interval
        self._bar_days = None if interval is None else interval.seconds / 86_400.0

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

    def enable_shorts(self, margin: MarginModel | None, borrow: BorrowSource | None = None) -> None:
        """Allow short sales under ``margin`` (a model that allows shorts)
        and ``borrow`` (default: keep the current source)."""
        if margin is None or not margin.allows_short:
            name = "none" if margin is None else margin.name
            raise ValueError(f"short selling needs a margin model that allows shorts, got {name}")
        self._margin = margin
        if borrow is not None:
            self._borrow = borrow
        self._allow_short = True

    @property
    def allow_short(self) -> bool:
        return self._allow_short

    @property
    def margin(self) -> MarginModel | None:
        """The margin model (``None``: a cash account)."""
        return self._margin

    @property
    def borrow(self) -> BorrowSource:
        return self._borrow

    @property
    def financing(self) -> tuple[FinancingEvent, ...]:
        """Every financing charge, in order (read-only)."""
        return tuple(self._financing)

    def average_cost(self, ticker: str) -> float | None:
        """Average entry price of the held position (``None`` when flat)."""
        return self._avg_cost.get(ticker)

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
        if _rests(order):
            self._resting.setdefault(order.client_id, order)
            self._unfilled[order.client_id] = 0.0
            return None

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
        return self._execute(order, decision.price, decision.quantity)

    def _execute(self, order: Order, price: float, quantity: float) -> Fill | None:
        """Fill ``quantity`` of ``order`` at reference ``price`` (before
        costs), within cash, margin and the held position."""
        requested = quantity
        cost = self._cost(order, price, quantity)
        held = self._portfolio.positions.get(order.ticker, 0.0)
        close_qty = quantity
        if self._margin is not None:
            sized = self._size_on_margin(order, price, quantity, cost, held)
            if sized is None:
                self._unfilled[order.client_id] = 0.0
                return None
            if sized[0] != quantity:
                self._unfilled[order.client_id] = 0.0
            quantity, close_qty, cost = sized
        elif order.side == "buy":
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
                    requested=requested,
                    filled=quantity,
                )
        else:  # sell
            if 0 < held < quantity <= held * (1 + _DUST):
                # float dust (0.1 + 0.2 > 0.3): sell exactly what is held
                quantity = close_qty = held
                cost = self._cost(order, price, quantity)
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
        self._track_cost(fill, held)
        self._portfolio.apply_fill(fill)
        if order.side == "sell" and self._settlement_days:
            proceeds = close_qty * cost.fill_price - cost.fee
            if proceeds > 0:
                self._unsettled.append((self._settles_on(fill.filled_at), proceeds))
        self._fills_by_client_id[order.client_id] = fill
        self._fills_order.append(fill)
        self._reference_prices[order.client_id] = price
        return fill

    # ---- resting stops (roadmap 19.10) -------------------------------------------

    def resting_orders(self) -> tuple[Order, ...]:
        """The good till cancelled stops still waiting, oldest first."""
        return tuple(self._resting.values())

    def cancel_order(self, client_id: str) -> bool:
        """Cancel a resting stop. ``False`` when there is none."""
        return self._resting.pop(client_id, None) is not None

    def trigger_resting(self) -> list[Fill]:
        """Fill every resting stop the current bar reaches (see the module
        doc). Returns the new fills, oldest stop first."""
        fills: list[Fill] = []
        for client_id, order in list(self._resting.items()):
            open_ = self._prices.get(order.ticker)
            if open_ is None or open_ <= 0:
                continue
            price = triggered_price(
                order, open_, self._highs.get(order.ticker), self._lows.get(order.ticker)
            )
            if price is None:
                continue
            del self._resting[client_id]
            held = self._portfolio.positions.get(order.ticker, 0.0)
            room = held if order.side == "sell" else -held
            quantity = min(order.quantity, room)
            if quantity <= 0:
                _log.debug("stop_dropped", client_id=client_id, reason="nothing_to_close")
                continue
            fill = self._execute(replace(order, quantity=quantity), price, quantity)
            if fill is not None:
                fills.append(fill)
        return fills

    def unfilled_quantity(self, client_id: str) -> float:
        """Quantity of ``client_id`` the fill model deferred to a later bar
        (participation cap, zero volume); ``0`` when it filled in full,
        expired, was cut by cash or position (a buy scaled to cash, a
        rejected sell), or was never placed."""
        return self._unfilled.get(client_id, 0.0)

    # ---- margin, borrow and financing ---------------------------------------

    def accrue(self, as_of: date, *, since: date | None = None) -> list[FinancingEvent]:
        """Charge financing for the calendar days from the last accrual (or
        ``since``) to ``as_of``, at the current prices: the borrow fee of
        every short and interest on negative cash. The first call only sets
        the start. Returns the new charges."""
        day = _day(as_of)
        last = since if since is not None else self._accrued_through
        if self._accrued_through is None or day > self._accrued_through:
            self._accrued_through = day
        if last is None or day <= last:
            return []
        days = (day - last).days
        stamp = _utc(as_of)
        events: list[FinancingEvent] = []
        for ticker, qty in sorted(self._portfolio.positions.items()):
            price = self._prices.get(ticker)
            if qty >= 0 or not price or price <= 0:
                continue
            quote = self._borrow.quote(ticker, day, self._asset_classes.get(ticker, "equity"))
            fee = 0.0 if quote is None else daily_fee(qty, price, quote, days)
            if fee > 0:
                events.append(FinancingEvent(stamp, ticker, "borrow_fee", -fee, days))
        rate = self._margin.debit_rate_annual if self._margin is not None else 0.0
        if rate > 0 and self._portfolio.cash < 0:
            interest = -self._portfolio.cash * rate * days / DAY_COUNT
            events.append(FinancingEvent(stamp, None, "debit_interest", -interest, days))
        for event in events:
            self._portfolio.cash += event.amount
        self._financing.extend(events)
        return events

    def margin_deficit(self) -> float:
        """How far equity sits below the maintenance requirement at the
        current prices (0 for a cash account or a book in good standing)."""
        if self._margin is None:
            return 0.0
        return self._margin.deficit(self._portfolio, self._prices, self._asset_classes)

    def margin_call(self) -> list[tuple[str, float]]:
        """``(ticker, signed quantity to trade)`` closes that cure the
        maintenance deficit at the current prices: the most losing
        position (vs its average cost) first, each closed only as far as
        needed. Empty when there is no deficit."""
        deficit = self.margin_deficit()
        if deficit <= 0 or self._margin is None:
            return []
        held = [
            (t, q, self._prices[t])
            for t, q in self._portfolio.positions.items()
            if q and self._prices.get(t, 0.0) > 0
        ]

        def pnl(item: tuple[str, float, float]) -> float:
            ticker, qty, price = item
            return (price - self._avg_cost.get(ticker, price)) * qty

        plan: list[tuple[str, float]] = []
        for ticker, qty, price in sorted(held, key=lambda i: (pnl(i), i[0])):
            if deficit <= 0:
                break
            asset_class = self._asset_classes.get(ticker, "equity")
            close = self._margin.cover_quantity(qty, price, deficit, asset_class)
            if close <= 0:
                continue
            plan.append((ticker, close if qty < 0 else -close))
            deficit -= close * price * self._margin.maintenance_rate(qty, asset_class)
        return plan

    def recalled(self, as_of: date) -> list[str]:
        """Held shorts the borrow source no longer quotes as borrowable on
        ``as_of`` (only sources with history can recall)."""
        if not self._borrow.has_history:
            return []
        day = _day(as_of)
        out = []
        for ticker, qty in sorted(self._portfolio.positions.items()):
            if qty >= 0:
                continue
            quote = self._borrow.quote(ticker, day, self._asset_classes.get(ticker, "equity"))
            if quote is None or not quote.shortable:
                out.append(ticker)
        return out

    def rescale_cost(self, ticker: str, ratio: float) -> None:
        """A split of ``ratio``: the average cost divides by it."""
        if ticker in self._avg_cost and ratio > 0:
            self._avg_cost[ticker] /= ratio

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
                periods_per_year=self._periods_per_year(order.ticker),
            )
        )

    def _periods_per_year(self, ticker: str) -> float | None:
        if self._interval is None:
            return None
        asset_class = self._asset_classes.get(ticker, "equity")
        return calendar_for(asset_class).periods_per_year(self._interval)

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
        if lo <= 0:
            return 0.0, cost  # even the fee is unaffordable (RS-33)
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

    def _size_on_margin(
        self, order: Order, price: float, quantity: float, cost: TradeCost, held: float
    ) -> tuple[float, float, TradeCost] | None:
        """``(quantity, closing part, cost)`` of an order on a margin
        account, or ``None`` when nothing may fill. The closing part is
        never limited; the opening part must be borrowable (short sales)
        and fit the excess equity left after the closing part."""
        margin = self._margin
        assert margin is not None
        requested = quantity
        sign = 1.0 if order.side == "buy" else -1.0
        closable = max(-held, 0.0) if sign > 0 else max(held, 0.0)
        if order.position_effect == "open" and closable > 0:
            # classified against a position that has since changed side:
            # filling it would first close that position (BE-13)
            _log.debug("order_rejected", client_id=order.client_id, reason="open_on_wrong_side")
            return None
        close_qty = min(quantity, closable)
        if close_qty > 0 and quantity - close_qty <= _DUST * quantity:
            # float dust past the holding: fill exactly the holding (BE-31)
            quantity = close_qty
        if order.position_effect == "close":
            # a close never opens the other side (BE-13)
            if close_qty <= 0:
                _log.debug("order_rejected", client_id=order.client_id, reason="nothing_to_close")
                return None
            quantity = close_qty
        open_qty = quantity - close_qty
        if open_qty > 0 and sign < 0:
            open_qty = self._borrowable(order, open_qty)
        if open_qty > 0:
            after_close = Portfolio(self._portfolio.cash, dict(self._portfolio.positions))
            if close_qty > 0:
                after_close.apply_fill(
                    Fill(
                        order.client_id,
                        order.ticker,
                        close_qty,
                        cost.fill_price,
                        0.0,
                        self._fill_time(),
                        order.side,
                    )
                )
            asset_class = self._asset_classes.get(order.ticker, "equity")
            excess = margin.excess_equity(after_close, self._prices, self._asset_classes)
            per_share = margin.initial_requirement(order.ticker, sign, cost.fill_price, asset_class)
            room = max(excess - cost.fee, 0.0) / per_share
            if open_qty > room:
                _log.debug(
                    "order_scaled_to_margin",
                    client_id=order.client_id,
                    requested=open_qty,
                    filled=room,
                )
                open_qty = room
        total = close_qty + open_qty
        if total <= 0:
            _log.debug("order_rejected", client_id=order.client_id, reason="insufficient_margin")
            return None
        if total != requested:
            cost = self._cost(order, price, total)
        return total, close_qty, cost

    def _borrowable(self, order: Order, quantity: float) -> float:
        """The part of a short sale of ``quantity`` that can be borrowed."""
        if not self._allow_short:
            _log.debug("order_rejected", client_id=order.client_id, reason="short_not_allowed")
            return 0.0
        day = _day(self._as_of) if self._as_of is not None else datetime.now(UTC).date()
        asset_class = self._asset_classes.get(order.ticker, "equity")
        quote = self._borrow.quote(order.ticker, day, asset_class)
        if quote is None or not quote.shortable:
            _log.debug("order_rejected", client_id=order.client_id, reason="not_borrowable")
            return 0.0
        if quote.available_shares is not None:
            return min(quantity, max(quote.available_shares, 0.0))
        return quantity

    def _track_cost(self, fill: Fill, held: float) -> None:
        """Keep the average entry price of the position ``fill`` changes."""
        signed = fill.signed_quantity
        after = held + signed
        if abs(after) <= _DUST * max(abs(held), fill.quantity):
            self._avg_cost.pop(fill.ticker, None)
        elif held == 0 or (held > 0) != (after > 0):
            self._avg_cost[fill.ticker] = fill.price  # opened or flipped
        elif (signed > 0) == (held > 0):  # grew
            old = self._avg_cost.get(fill.ticker, fill.price)
            self._avg_cost[fill.ticker] = (old * abs(held) + fill.price * fill.quantity) / abs(
                after
            )

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


def _rests(order: Order) -> bool:
    """A good till cancelled stop rests until a bar reaches it."""
    return order.order_type in ("stop", "stop_limit") and order.time_in_force == "gtc"


def _day(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _utc(value: date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)
