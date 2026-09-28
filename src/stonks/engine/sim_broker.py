"""The simulated intraday broker: next-bar fills on minute bars (roadmap 21.2.3).

A paper or backtest book of the intraday engine trades here. It behaves
like a live broker, so the router and reconciliation treat it the same way
as IBKR:

- ``place_order`` never fills. The order waits for the next bar of its
  ticker (P21): a decision on the close of minute t fills at the open of
  minute t+1. A bar that began before the decision bar closed is never used.
- On each bar close (:meth:`IntradaySimBroker.on_bar_close`, fed by the
  router) every working order meets the new bar through the
  :class:`~stonks.backtest.fills.MinuteFillModel`: the participation cap of
  the minute's volume (P20), limit and stop orders from the bar range, the
  half spread of the last recorded quote before the open, and day orders
  that expire in a new session. What the cap defers stays working.
- Cash, margin, positions and costs come from a wrapped
  :class:`~stonks.backtest.simulated_broker.SimulatedBroker`, so the cost
  model, the cash check and short rules are the daily ones.
- Every fill is an :class:`~stonks.execution.brokers.base.Execution` with
  its own id, and the cost model's fee is its commission.
  ``get_order_state`` reports the cumulative state. That is what
  ``execution.reconcile`` reads from IBKR, so fills reach the ledger by the
  same path (``ExecutionSource`` plus ``OrderStateSource``).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from stonks.backtest.costs import CostModel
from stonks.backtest.fills import (
    BarQuote,
    FillDecision,
    MarketStatsSpec,
    MinuteFillModel,
    MinuteFillSettings,
)
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.interval import Interval
from stonks.core.stream import QuoteTick
from stonks.core.types import AssetClass, Fill, Order, Portfolio
from stonks.engine.driver import BarClose
from stonks.execution.brokers.base import QTY_EPSILON, BrokerOrderState, Execution, OrderState
from stonks.execution.order_state import TERMINAL, ledger_status
from stonks.logging import get_logger

_log = get_logger("stonks.engine.sim_broker")

#: Bars of quotes the broker keeps per ticker (plus the newest older one).
QUOTE_WINDOW_BARS = 5

#: The session a bar belongs to (any value that compares equal within one
#: session), or ``None`` outside every session.
SessionKey = Callable[[str, datetime], object]


def calendar_session_key(ticker: str, at: datetime) -> date | None:
    """The local date of the exchange session holding ``at``, from the
    ticker's calendar. ``None`` outside regular hours. A ticker with no
    known calendar falls back to the UTC date."""
    from stonks.engine.sessions import session_at
    from stonks.scheduling.calendar import (
        CalendarRangeError,
        UnknownCalendarError,
        calendar_for_ticker,
    )

    try:
        session = session_at(calendar_for_ticker(ticker), at)
    except (UnknownCalendarError, CalendarRangeError):
        return at.astimezone(UTC).date()
    return None if session is None else session.date


@dataclass
class _Working:
    order: Order
    broker_order_id: str
    #: The close of the decision bar: the first bar that may fill starts here.
    decided_bar: datetime
    remaining: float
    state: OrderState = "accepted"
    filled: float = 0.0
    notional: float = 0.0
    attempts: int = 0
    reason: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class _Context:
    bid: float | None
    ask: float | None
    gap_bars: float
    new_session: bool


class _ContextFill:
    """The minute model, with the per-order bar context the wrapped
    ``SimulatedBroker`` does not know about (quote, gap, session)."""

    def __init__(self, model: MinuteFillModel) -> None:
        self.model = model
        self.context: _Context | None = None
        self.last_reason: str | None = None

    @property
    def market_stats_spec(self) -> MarketStatsSpec:
        return self.model.market_stats_spec

    def decide(self, order: Order, quote: BarQuote) -> FillDecision:
        ctx = self.context
        if ctx is not None:
            quote = replace(
                quote,
                bid=ctx.bid,
                ask=ctx.ask,
                gap_bars=ctx.gap_bars,
                new_session=ctx.new_session,
            )
        decision = self.model.decide(order, quote)
        self.last_reason = decision.reason
        return decision


def floor_to(at: datetime, step: timedelta) -> datetime:
    """``at`` rounded down to a multiple of ``step`` since the epoch (UTC)."""
    seconds = step.total_seconds()
    ts = at.astimezone(UTC).timestamp()
    return datetime.fromtimestamp((ts // seconds) * seconds, UTC)


class IntradaySimBroker:
    """See the module doc. One instance trades one book."""

    def __init__(
        self,
        portfolio: Portfolio,
        *,
        fill: MinuteFillSettings | None = None,
        cost_model: CostModel | None = None,
        interval: Interval = Interval.MIN_1,
        session_key: SessionKey | None = calendar_session_key,
        clock: Clock = SYSTEM_CLOCK,
        order_prefix: str = "sim",
    ) -> None:
        self.clock = clock
        #: Broker order ids (and so execution ids) start with this. A book
        #: that restarts needs a new prefix, or its new execution ids would
        #: repeat the ones already booked (21.2.5).
        self.order_prefix = order_prefix
        self.interval = interval
        self._step = interval.to_timedelta()
        self._session_key = session_key
        self._fill = _ContextFill(MinuteFillModel(fill))
        self._sim = SimulatedBroker(portfolio, cost_model=cost_model, fill_model=self._fill)
        self._sim.set_interval(interval)
        self._orders: dict[str, _Working] = {}
        self._executions: list[Execution] = []
        #: Recent quotes per ticker, oldest first: live, quotes keep coming
        #: while a fill bar forms, and the fill wants the last one before
        #: that bar opened.
        self._quotes: dict[str, deque[QuoteTick]] = {}
        self._quote_window = self._step * QUOTE_WINDOW_BARS

    # ---- market data -----------------------------------------------------------------

    def set_asset_classes(self, asset_classes: Mapping[str, AssetClass]) -> None:
        self._sim.set_asset_classes(asset_classes)

    def on_quote(self, tick: QuoteTick) -> None:
        """Remember a recorded quote of a ticker (for the half spread). The
        last few bars of quotes are kept, and always the newest one older
        than that, so a fill finds the quote before its bar opened."""
        quotes = self._quotes.setdefault(tick.ticker, deque())
        if quotes and tick.timestamp < quotes[-1].timestamp:
            items = sorted([*quotes, tick], key=lambda q: q.timestamp)
            quotes.clear()
            quotes.extend(items)
        else:
            quotes.append(tick)
        cutoff = quotes[-1].timestamp - self._quote_window
        while len(quotes) > 1 and quotes[1].timestamp <= cutoff:
            quotes.popleft()

    def on_bar_close(self, event: BarClose) -> list[Execution]:
        """Fill the working orders against the bars of ``event``. Returns
        the new executions."""
        bars = {b.ticker: b for b in event.bars}
        if not bars:
            return []
        start = event.at - self._step
        self._sim.set_prices(
            {t: b.open for t, b in bars.items()},
            start,
            {t: float(b.volume) for t, b in bars.items()},
            highs={t: b.high for t, b in bars.items()},
            lows={t: b.low for t, b in bars.items()},
        )
        new: list[Execution] = []
        for work in list(self._orders.values()):
            bar = bars.get(work.order.ticker)
            if work.state in TERMINAL or bar is None or bar.timestamp < work.decided_bar:
                continue
            execution = self._try_fill(work, bar.timestamp)
            if execution is not None:
                new.append(execution)
        return new

    # ---- Broker ----------------------------------------------------------------------

    def fetch_portfolio(self) -> Portfolio:
        return self._sim.fetch_portfolio()

    def place_order(self, order: Order) -> Fill | None:
        """Queue ``order`` for the next bar. Never fills now. The same
        client id again is a no-op."""
        if order.client_id in self._orders:
            return None
        decided = order.decided_at or self.clock.now()
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=UTC)
        work = _Working(
            order=order,
            broker_order_id=f"{self.order_prefix}-{len(self._orders) + 1}",
            decided_bar=floor_to(decided, self._step),
            remaining=order.quantity,
            updated_at=self.clock.now(),
        )
        self._orders[order.client_id] = work
        return None

    def reconcile(self) -> list[Fill]:
        return [e.to_fill() for e in self._executions]

    # ---- ExecutionSource / OrderStateSource / OrderCanceller ----------------------------

    def executions(self, since: datetime) -> list[Execution]:
        return [e for e in self._executions if e.executed_at >= since]

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        work = self._orders.get(client_id)
        if work is None:
            return None
        order = work.order
        return BrokerOrderState(
            client_id=client_id,
            broker_order_id=work.broker_order_id,
            ticker=order.ticker,
            side=order.side,
            status=ledger_status(work.state),
            quantity=order.quantity,
            filled_quantity=work.filled,
            avg_fill_price=work.notional / work.filled if work.filled > 0 else None,
            updated_at=work.updated_at,
            state=work.state,
        )

    def cancel_order(self, client_id: str) -> bool:
        work = self._orders.get(client_id)
        if work is None or work.state in TERMINAL:
            return False
        self._end(work, "cancelled", "cancelled")
        return True

    def expire_open(self, reason: str = "session_end") -> list[str]:
        """End every working order (a day order at the close). Returns
        their client ids."""
        ended = [w for w in self._orders.values() if w.state not in TERMINAL]
        for work in ended:
            self._end(work, "expired", reason)
        return [w.order.client_id for w in ended]

    def open_client_ids(self) -> list[str]:
        return [cid for cid, w in self._orders.items() if w.state not in TERMINAL]

    # ---- internals -------------------------------------------------------------------

    def _try_fill(self, work: _Working, bar_start: datetime) -> Execution | None:
        order = work.order
        work.attempts += 1
        child = replace(order, client_id=f"{order.client_id}#{work.attempts}",
                        quantity=work.remaining)  # fmt: skip
        quote = self._quote_before(order.ticker, bar_start)
        self._fill.context = _Context(
            bid=quote.bid if quote else None,
            ask=quote.ask if quote else None,
            gap_bars=(bar_start - work.decided_bar) / self._step,
            new_session=self._new_session(order.ticker, work.decided_bar, bar_start),
        )
        try:
            fill = self._sim.place_order(child)
        finally:
            self._fill.context = None
        carry = self._sim.unfilled_quantity(child.client_id)
        execution = None
        if fill is not None and fill.quantity > QTY_EPSILON:
            execution = Execution(
                broker_exec_id=f"{work.broker_order_id}.{work.attempts}",
                client_id=order.client_id,
                ticker=order.ticker,
                side=order.side,
                quantity=fill.quantity,
                price=fill.price,
                executed_at=fill.filled_at,
                commission=fill.fee,
            )
            self._executions.append(execution)
            work.filled += fill.quantity
            work.notional += fill.quantity * fill.price
        work.updated_at = bar_start
        if work.filled >= order.quantity - QTY_EPSILON * max(1.0, order.quantity):
            work.remaining = 0.0
            work.state = "filled"
        elif carry > QTY_EPSILON:
            work.remaining = carry
            work.state = "partially_filled" if work.filled > 0 else "accepted"
        else:
            self._end(work, "expired", self._fill.last_reason or "not filled")
        return execution

    def _end(self, work: _Working, state: OrderState, reason: str) -> None:
        work.state = state
        work.reason = reason
        work.remaining = 0.0
        work.updated_at = self.clock.now()
        _log.debug("sim.order_ended", client_id=work.order.client_id, state=state, reason=reason)

    def _quote_before(self, ticker: str, at: datetime) -> QuoteTick | None:
        for quote in reversed(self._quotes.get(ticker, ())):
            if quote.timestamp <= at:
                return quote
        return None

    def _new_session(self, ticker: str, decided_bar: datetime, bar_start: datetime) -> bool:
        if self._session_key is None:
            return False
        # the decision bar started one step before its close
        before = self._session_key(ticker, decided_bar - self._step)
        return before != self._session_key(ticker, bar_start)
