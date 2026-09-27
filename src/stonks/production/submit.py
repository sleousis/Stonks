"""The ``live_submit`` job: send approved order tickets in the submit window
(roadmap 19.8, design ``docs/design/live-trading.md`` sections 3 and 6).

For each portfolio with tickets due:

1. Open the broker. A broker that cannot be opened leaves the tickets as
   they are. They expire at the deadline and the next tick decides afresh.
2. The startup reconciliation gate: reconcile every open order by client
   id, then :func:`~stonks.execution.order_state.require_reconciled`. While
   any order is ``unknown``, nothing is sent.
3. The halts in force now: a halt of new orders holds every ticket, a halt
   of buys holds the tickets that open or grow a position. A held ticket
   stays approved until the deadline.
4. Each ticket becomes its order, with the ticket's client id: the row is
   committed ``pending`` first, then sent, then ``submitted``. A rejection
   fails the ticket. A submit with no answer is ``unknown`` until
   reconciliation settles it, never sent twice.

Then every submitted ticket follows its order (filled, unfilled,
cancelled, failed). Tickets past their deadline expire first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock, today
from stonks.core.protocols import Broker
from stonks.execution.brokers.base import OrderRejectedError, OrderStateSource
from stonks.execution.order_state import (
    ReconciliationPendingError,
    current_state,
    mark_unknown,
    require_reconciled,
    write_state,
)
from stonks.execution.reconcile import reconcile_order, startup_reconcile
from stonks.logging import get_logger
from stonks.production.halts import active_halts
from stonks.production.tickets import (
    Ticket,
    due_tickets,
    expire_due,
    set_ticket_status,
    sync_submitted,
)
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.submit")

#: Opens the broker a portfolio trades through.
BrokerOpener = Callable[[str], Broker]

PortfolioSubmitStatus = Literal["ok", "partial", "skipped", "error"]


@dataclass(frozen=True)
class PortfolioSubmit:
    portfolio_id: str
    status: PortfolioSubmitStatus
    #: Tickets sent to the broker.
    sent: int = 0
    #: Tickets the broker rejected, or whose submit had no answer.
    failed: int = 0
    #: Tickets a halt kept back (they stay approved until the deadline).
    held: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    portfolios: tuple[PortfolioSubmit, ...] = ()
    #: Tickets that passed their deadline unsent.
    expired: tuple[str, ...] = ()
    #: Submitted tickets that reached their outcome.
    settled: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def sent(self) -> int:
        return sum(p.sent for p in self.portfolios)

    @property
    def failed(self) -> int:
        return sum(p.failed for p in self.portfolios)


def submit_tickets(
    state: SqliteState,
    open_broker: BrokerOpener,
    *,
    clock: Clock = SYSTEM_CLOCK,
    portfolio_id: str | None = None,
) -> SubmitResult:
    """Send every approved ticket inside its window (of ``portfolio_id``
    when given). Never raises for one portfolio's broker."""
    now = clock.now()
    expired = tuple(expire_due(state, now))
    due = due_tickets(state, now, portfolio_id=portfolio_id)
    by_portfolio: dict[str, list[Ticket]] = {}
    for ticket in due:
        by_portfolio.setdefault(ticket.portfolio_id, []).append(ticket)
    results = [
        _submit_portfolio(state, pid, tickets, open_broker, clock)
        for pid, tickets in by_portfolio.items()
    ]
    settled = sync_submitted(state, now=clock.now())
    result = SubmitResult(portfolios=tuple(results), expired=expired, settled=settled)
    _log.info(
        "submit.done",
        portfolios=len(results),
        sent=result.sent,
        failed=result.failed,
        expired=len(expired),
        settled=settled,
    )
    return result


def _submit_portfolio(
    state: SqliteState,
    portfolio_id: str,
    tickets: list[Ticket],
    open_broker: BrokerOpener,
    clock: Clock,
) -> PortfolioSubmit:
    log = _log.bind(portfolio_id=portfolio_id)
    try:
        broker = open_broker(portfolio_id)
    except Exception as exc:
        log.warning("submit.broker_unavailable", error=str(exc))
        return PortfolioSubmit(portfolio_id, "error", reason=f"{type(exc).__name__}: {exc}")
    if not isinstance(broker, OrderStateSource):
        return PortfolioSubmit(
            portfolio_id, "error", reason="the broker cannot look orders up by client id"
        )
    try:
        startup = startup_reconcile(broker, state, portfolio_id=portfolio_id, clock=clock)
        require_reconciled(state, portfolio_id)
    except ReconciliationPendingError as exc:
        log.warning("submit.unreconciled", client_ids=list(exc.client_ids))
        return PortfolioSubmit(portfolio_id, "skipped", reason=str(exc))
    except Exception as exc:
        log.warning("submit.reconcile_failed", error=str(exc))
        return PortfolioSubmit(portfolio_id, "error", reason=f"{type(exc).__name__}: {exc}")
    if startup.summary.failed_orders:
        # An order that could not be looked up may be a crashed send (a
        # pending row the broker never saw) or one it has: the window waits
        # until the startup reconciliation is clean (StartupReconcile.ok).
        failed_ids = list(startup.summary.failed_orders)
        log.warning("submit.reconcile_partial", failed=len(failed_ids))
        return PortfolioSubmit(
            portfolio_id,
            "skipped",
            reason=f"{len(failed_ids)} order(s) could not be looked up at the broker",
        )

    owner = state.sql("SELECT owner_id FROM portfolios WHERE id = ?", [portfolio_id])
    halts = active_halts(
        state,
        today(clock),
        portfolio_id=portfolio_id,
        user_id=owner[0]["owner_id"] if owner else None,
    )
    stop_all = any(h.halt == "all" for h in halts) or not _book_running(state, portfolio_id)
    stop_buys = any(h.halt == "buys" for h in halts)
    running = _running_strategies(state, portfolio_id)

    sent = failed = held = 0
    for ticket in tickets:
        stopped = ticket.strategy_id is not None and ticket.strategy_id not in running
        if stop_all or stopped or (stop_buys and not _reduces(ticket)):
            held += 1
            continue
        outcome = _send(state, broker, ticket, clock)
        sent += outcome == "sent"
        failed += outcome == "failed"
    if held:
        log.warning("submit.held_by_halt", held=held)
    status: PortfolioSubmitStatus = "partial" if failed else "ok"
    return PortfolioSubmit(portfolio_id, status, sent=sent, failed=failed, held=held)


def _book_running(state: SqliteState, portfolio_id: str) -> bool:
    """The portfolio is active and its owner is not disabled. A book stopped
    after the tick decided sends nothing (its tickets expire)."""
    rows = state.sql(
        "SELECT p.status AS portfolio_status, u.status AS user_status FROM portfolios p"
        " LEFT JOIN users u ON u.id = p.owner_id WHERE p.id = ?",
        [portfolio_id],
    )
    if not rows:
        return False
    return rows[0]["portfolio_status"] == "active" and rows[0]["user_status"] in (None, "active")


def _running_strategies(state: SqliteState, portfolio_id: str) -> set[str]:
    """Strategies with a running live subscription on ``portfolio_id``:
    enabled, approve or auto, not paused, on an active strategy. A ticket
    of any other strategy (paused or retired after the tick) is held."""
    rows = state.sql(
        "SELECT s.strategy_id FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id"
        " WHERE s.portfolio_id = ? AND s.enabled = 1 AND s.mode IN ('approve', 'auto')"
        " AND s.paused_reason IS NULL AND st.status = 'active'",
        [portfolio_id],
    )
    return {r["strategy_id"] for r in rows}


def _reduces(ticket: Ticket) -> bool:
    from stonks.production.tick import _reduces as reduces

    return reduces(ticket.order)


def _send(
    state: SqliteState, broker: Broker, ticket: Ticket, clock: Clock
) -> Literal["sent", "failed", "known"]:
    """Send one ticket's order. ``known``: the order was sent before (a
    crash after the send): the ticket just follows it."""
    from stonks.production.tick import _record_order

    order = ticket.order
    cid = order.client_id
    existing = current_state(state, cid)
    if existing is not None and existing not in ("rejected", "cancelled"):
        set_ticket_status(state, ticket.id, "submitted", now=clock.now())
        return "known"
    with state.transaction():
        _record_order(state, order, status="pending", portfolio_id=ticket.portfolio_id)
    try:
        broker.place_order(order)
    except OrderRejectedError as exc:
        _log.warning("submit.rejected", client_id=cid, error=str(exc))
        with state.transaction():
            write_state(state, cid, "rejected", reason=str(exc)[:500], clock=clock)
            set_ticket_status(state, ticket.id, "failed", now=clock.now(), reason=str(exc))
        return "failed"
    except Exception as exc:
        # it may or may not have reached the broker: nothing is sent for it
        # again until reconciliation settles it by client id
        _log.warning("submit.no_answer", client_id=cid, error=str(exc))
        with state.transaction():
            mark_unknown(state, cid, f"submit outcome unknown: {exc}"[:500], clock=clock)
            set_ticket_status(
                state, ticket.id, "submitted", now=clock.now(), reason="outcome unknown"
            )
        return "failed"
    with state.transaction():
        write_state(state, cid, "submitted", clock=clock)
        set_ticket_status(state, ticket.id, "submitted", now=clock.now())
    try:
        assert isinstance(broker, OrderStateSource)
        reconcile_order(broker, state, cid, reject_unknown=False)
    except Exception as exc:
        _log.warning("submit.sync_failed", client_id=cid, error=str(exc))
    return "sent"
