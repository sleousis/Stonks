"""The intraday router: orders in, acknowledgements out (roadmap 21.2.3).

The decision step (21.2.2, ``engine/step.py``) builds a book's orders on a
bar close and hands them to an :class:`OrderRouter`. The router is the only
part of the engine that talks to a broker. One router serves one book (one
portfolio and its broker).

The interface the step calls::

    acks = router.route(orders)   # Sequence[Order] -> tuple[RouteAck, ...]

One :class:`RouteAck` per order, in order. ``route`` never raises for one
order, and it never fills: fills arrive on a later event. ``ack.accepted``
says whether the order is at the broker. The step reads nothing else.

:class:`IntradayRouter` does the work, the same way for the simulated
intraday broker (``engine/sim_broker.py``) and for IBKR:

- **Before trading** :meth:`IntradayRouter.start` runs the startup
  reconciliation (``execution.reconcile.startup_reconcile``) and
  ``require_reconciled``. Until it passes, every order is ``held``.
- **Day orders.** Every order goes out as a day order
  (:func:`as_day_order`): no time in force means ``day``. The opening
  auction and good till cancelled are refused, since an intraday book ends
  with the session. The IBKR adapter checks the same rule again
  (``IbkrBroker(intraday=True)``).
- **The state machine** (``execution.order_state``). The row is committed
  ``pending`` before the broker sees the order, then ``submitted``, then
  synced with the broker by client id. A rejection ends it ``rejected``. A
  submit with no answer is ``unknown``: nothing more is sent for that
  ticker until reconciliation settles it, while other tickers still trade.
  A client id already in the ledger is ``known`` and never sent twice.
- **Per event** :meth:`IntradayRouter.on_bar_close` lets the simulated
  broker fill its working orders against the new bars (next-bar fills, P21),
  then reconciles the book's open orders through
  ``execution.reconcile.reconcile_orders``. Both brokers report executions
  and order state, so fills are booked once per execution id, with fees.

Register the router on the :class:`~stonks.engine.driver.EventDriver` at
:data:`ROUTER_PRIORITY`, before the step, so a bar fills the orders of the
previous close before anything new is decided on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol, runtime_checkable

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.protocols import Broker
from stonks.core.types import Order, TimeInForce
from stonks.engine.driver import BarClose
from stonks.execution.brokers.base import (
    ExecutionSource,
    OrderRejectedError,
    OrderState,
    OrderStateSource,
)
from stonks.execution.order_state import (
    current_state,
    mark_unknown,
    require_reconciled,
    unknown_orders,
    write_state,
)
from stonks.execution.reconcile import (
    NON_TERMINAL_STATUSES,
    ReconcileSummary,
    StartupReconcile,
    reconcile_order,
    reconcile_orders,
    startup_reconcile,
)
from stonks.logging import get_logger
from stonks.production.ledger import ledger_filter
from stonks.store.state import SqliteState

_log = get_logger("stonks.engine.router")

#: Driver priority of the router: after the session gates, before the step.
ROUTER_PRIORITY = -10
#: Times in force an intraday book may use: orders that end with the session.
INTRADAY_TIFS: frozenset[TimeInForce] = frozenset({"day", "ioc"})

RouteStatus = Literal["sent", "known", "rejected", "unknown", "held"]


@dataclass(frozen=True)
class RouteAck:
    """What happened to one routed order.

    ``status``: ``sent`` (at the broker now), ``known`` (the client id was
    routed before, nothing sent again), ``rejected`` (refused by the router
    or the broker), ``unknown`` (sent with no answer, settled by
    reconciliation) or ``held`` (not sent: the router is not started or the
    ticker waits for reconciliation). ``state`` is the order's state in the
    ledger afterwards (``None`` when no row was written)."""

    client_id: str
    ticker: str
    status: RouteStatus
    state: OrderState | None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        """Whether the order is at the broker."""
        return self.status in ("sent", "known")


@runtime_checkable
class OrderRouter(Protocol):
    """The interface the decision step calls. See the module doc."""

    def route(self, orders: Sequence[Order]) -> tuple[RouteAck, ...]: ...


def as_day_order(order: Order) -> Order:
    """``order`` as an intraday day order: no time in force becomes
    ``day``. Raises ``OrderRejectedError`` for one that would outlive the
    session (``opg``, ``gtc``)."""
    tif = order.time_in_force or "day"
    if tif not in INTRADAY_TIFS:
        raise OrderRejectedError(
            f"{order.client_id}: an intraday book sends day orders only, not {tif}"
        )
    return order if order.time_in_force == tif else replace(order, time_in_force=tif)


class IntradayRouter:
    """Routes one book's orders. See the module doc."""

    name = "router"

    def __init__(
        self,
        broker: Broker,
        state: SqliteState,
        *,
        portfolio_id: str = DEFAULT_PORTFOLIO_ID,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if not isinstance(broker, OrderStateSource):
            raise TypeError("the intraday router needs a broker that looks orders up by client id")
        self.broker = broker
        self.state = state
        self.portfolio_id = portfolio_id
        self.clock = clock
        self.ready = False
        self.last_reconcile: ReconcileSummary | None = None
        self._log = _log.bind(portfolio_id=portfolio_id)

    # ---- lifecycle --------------------------------------------------------------------

    def start(self) -> StartupReconcile:
        """Reconcile every open order, then open the router. Raises
        ``ReconciliationPendingError`` while an order stays ``unknown``."""
        self.ready = False
        startup = startup_reconcile(
            self.broker, self.state, portfolio_id=self.portfolio_id, clock=self.clock
        )
        require_reconciled(self.state, self.portfolio_id)
        self.ready = True
        self.last_reconcile = startup.summary
        self._log.info("router.started", failed=len(startup.summary.failed_orders))
        return startup

    # ---- the interface ---------------------------------------------------------------

    def route(self, orders: Sequence[Order]) -> tuple[RouteAck, ...]:
        if not orders:
            return ()
        if not self.ready:
            reason = "the router is not started: run start() to reconcile first"
            return tuple(RouteAck(o.client_id, o.ticker, "held", None, reason) for o in orders)
        held = self._unknown_tickers()
        acks: list[RouteAck] = []
        for order in orders:
            ack = self._route_one(order, held)
            if ack.status == "unknown":
                held.add(order.ticker)
            acks.append(ack)
        return tuple(acks)

    def on_bar_close(self, event: BarClose) -> ReconcileSummary:
        """Fill simulated orders against the new bars, then reconcile the
        book's open orders."""
        fill_on_bar = getattr(self.broker, "on_bar_close", None)
        if callable(fill_on_bar):
            fill_on_bar(event)
        if not self._has_open_orders():
            summary = ReconcileSummary()
        else:
            summary = reconcile_orders(
                self.broker, self.state, now=self.clock.now(), portfolio_id=self.portfolio_id
            )
        self.last_reconcile = summary
        return summary

    # ---- internals -------------------------------------------------------------------

    def _route_one(self, order: Order, held: set[str]) -> RouteAck:
        from stonks.production.tick import _record_order

        cid = order.client_id
        existing = current_state(self.state, cid)
        if existing is not None:
            return RouteAck(cid, order.ticker, "known", existing)
        if order.ticker in held:
            reason = f"{order.ticker} has an order in an unknown state: reconcile first"
            return RouteAck(cid, order.ticker, "held", None, reason)
        try:
            order = as_day_order(order)
        except OrderRejectedError as exc:
            return RouteAck(cid, order.ticker, "rejected", None, str(exc))
        if order.decided_at is None:
            order = replace(order, decided_at=self.clock.now())
        with self.state.transaction():
            _record_order(self.state, order, status="pending", portfolio_id=self.portfolio_id)
        try:
            self.broker.place_order(order)
        except OrderRejectedError as exc:
            self._log.warning("router.rejected", client_id=cid, error=str(exc))
            write_state(self.state, cid, "rejected", reason=str(exc)[:500], clock=self.clock)
            return RouteAck(cid, order.ticker, "rejected", "rejected", str(exc))
        except Exception as exc:
            # it may or may not have reached the broker: nothing more is
            # sent for this ticker until reconciliation settles it
            self._log.warning("router.no_answer", client_id=cid, error=str(exc))
            mark_unknown(self.state, cid, f"submit outcome unknown: {exc}"[:500], clock=self.clock)
            return RouteAck(cid, order.ticker, "unknown", "unknown", str(exc))
        write_state(self.state, cid, "submitted", clock=self.clock)
        try:
            reconcile_order(
                self.broker,
                self.state,
                cid,
                now=self.clock.now(),
                reject_unknown=False,
                # fills of brokers with executions are booked from those only
                book_fill=not isinstance(self.broker, ExecutionSource),
            )
        except Exception as exc:
            self._log.warning("router.sync_failed", client_id=cid, error=str(exc))
        return RouteAck(cid, order.ticker, "sent", current_state(self.state, cid))

    def _unknown_tickers(self) -> set[str]:
        ids = unknown_orders(self.state, self.portfolio_id)
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        rows = self.state.sql(f"SELECT DISTINCT ticker FROM orders WHERE client_id IN ({marks})",
                              list(ids))  # fmt: skip
        return {r["ticker"] for r in rows}

    def _has_open_orders(self) -> bool:
        where, params = ledger_filter(self.state, "orders", self.portfolio_id)
        marks = ",".join("?" for _ in NON_TERMINAL_STATUSES)
        rows = self.state.sql(
            f"SELECT 1 FROM orders WHERE status IN ({marks}) AND {where} LIMIT 1",
            [*NON_TERMINAL_STATUSES, *params],
        )
        return bool(rows)
