"""The ``live_submit`` job: send approved order tickets in the submit window
(roadmap 19.8, design ``docs/design/live-trading.md`` sections 3 and 6).

For each portfolio with tickets due:

1. Open the broker. A broker that cannot be opened leaves the tickets as
   they are. They expire at the deadline and the next tick decides afresh.
2. The submit gate of 19.5 (:func:`~stonks.production.live.checks.submit_gate`):
   reconcile every open order by client id, compare the broker with the
   ledger and store a ``submit`` report. Nothing is sent unless
   :attr:`~stonks.production.live.checks.CheckResult.may_submit` (the broker
   answered, nothing material drifted, no order is still ``unknown``).
3. The halts in force now: a halt of new orders holds every ticket, a halt
   of buys holds the tickets that open or grow a position. A held ticket
   stays approved until the deadline.
4. The pre-open gap check (roadmap 19.14, ``production/live/gap.py``): an
   opening ticket whose latest pre-open quote moved beyond the portfolio's
   ``price_band`` since the decision is held, and the owner is alerted.
5. Each ticket becomes its order, with the ticket's client id: the row is
   committed ``pending`` first, then sent, then ``submitted``. A rejection
   fails the ticket. A submit with no answer is ``unknown`` until
   reconciliation settles it, never sent twice.

Then every submitted ticket follows its order (filled, unfilled,
cancelled, failed). Tickets past their deadline expire first.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock, today
from stonks.core.protocols import Broker
from stonks.execution.brokers.base import (
    OrderRejectedError,
    OrderStateSource,
    Quote,
    QuoteSource,
)
from stonks.execution.order_state import current_state, mark_unknown, write_state
from stonks.execution.reconcile import reconcile_order
from stonks.logging import get_logger
from stonks.production.halts import active_halts
from stonks.production.live.checks import CheckResult, Publish, submit_gate
from stonks.production.live.gap import gap_limit, is_opening, preopen_holds
from stonks.production.live.settings import LiveSettings
from stonks.production.tickets import (
    Ticket,
    due_tickets,
    expire_due,
    set_ticket_status,
    sync_submitted,
)
from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.config import RiskPolicy

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
    #: Tickets a halt or the gap check kept back (they stay approved until
    #: the deadline).
    held: int = 0
    reason: str | None = None
    #: The tickets the pre-open gap check held.
    gap_held: tuple[str, ...] = ()
    #: The ``submit`` reconcile report the gate stored.
    report_id: str | None = None


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
    risk: RiskPolicy | None = None,
    live: LiveSettings | None = None,
    publish: Publish | None = None,
) -> SubmitResult:
    """Send every approved ticket inside its window (of ``portfolio_id``
    when given). Never raises for one portfolio's broker.

    ``risk``: the global risk policy (``[production.risk]``). Each
    portfolio's own and its owner's limits tighten it, and its
    ``price_band`` sets the gap check. ``live``: ``[production.live]`` for
    the submit gate. ``publish``: where alerts go (the configured router by
    default)."""
    run = _Run(risk=risk, live=live or LiveSettings(), publish=publish)
    now = clock.now()
    expired = tuple(expire_due(state, now))
    due = due_tickets(state, now, portfolio_id=portfolio_id)
    by_portfolio: dict[str, list[Ticket]] = {}
    for ticket in due:
        by_portfolio.setdefault(ticket.portfolio_id, []).append(ticket)
    results = [
        _submit_portfolio(state, pid, tickets, open_broker, clock, run)
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


@dataclass(frozen=True)
class _Run:
    risk: RiskPolicy | None
    live: LiveSettings
    publish: Publish | None


def _submit_portfolio(
    state: SqliteState,
    portfolio_id: str,
    tickets: list[Ticket],
    open_broker: BrokerOpener,
    clock: Clock,
    run: _Run,
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
        gate = submit_gate(
            state, broker, portfolio_id, settings=run.live, clock=clock, publish=run.publish
        )
    except Exception as exc:
        log.warning("submit.gate_failed", error=str(exc))
        return PortfolioSubmit(portfolio_id, "error", reason=f"{type(exc).__name__}: {exc}")
    report_id = gate.report.id
    if not gate.may_submit:
        log.warning("submit.gate_closed", status=gate.status, report_id=report_id)
        closed: PortfolioSubmitStatus = "error" if gate.status in ("outage", "fault") else "skipped"
        return PortfolioSubmit(portfolio_id, closed, reason=_gate_reason(gate), report_id=report_id)

    rows = state.sql(
        "SELECT p.owner_id, p.risk_policy_json, u.risk_policy_json AS owner_risk_json"
        " FROM portfolios p LEFT JOIN users u ON u.id = p.owner_id WHERE p.id = ?",
        [portfolio_id],
    )
    owner = rows[0] if rows else None
    halts = active_halts(
        state,
        today(clock),
        portfolio_id=portfolio_id,
        user_id=owner["owner_id"] if owner else None,
    )
    stop_all = any(h.halt == "all" for h in halts) or not _book_running(state, portfolio_id)
    stop_buys = any(h.halt == "buys" for h in halts)
    running = _running_strategies(state, portfolio_id)

    sendable: list[Ticket] = []
    held = 0
    for ticket in tickets:
        stopped = ticket.strategy_id is not None and ticket.strategy_id not in running
        if stop_all or stopped or (stop_buys and not _reduces(ticket)):
            held += 1
            continue
        sendable.append(ticket)
    if held:
        log.warning("submit.held_by_halt", held=held)

    limit = gap_limit(_price_band(run.risk, owner))
    reasons = preopen_holds(
        [t.order for t in sendable], _preopen_quotes(broker, sendable, limit, log), limit
    )
    gapped = [t for t in sendable if t.client_id in reasons]
    if gapped:
        log.warning("submit.held_by_gap", tickets=[t.id for t in gapped])
        _alert_gaps(state, portfolio_id, gapped, reasons, clock, run.publish)

    sent = failed = 0
    for ticket in sendable:
        if ticket.client_id in reasons:
            continue
        outcome = _send(state, broker, ticket, clock)
        sent += outcome == "sent"
        failed += outcome == "failed"
    status: PortfolioSubmitStatus = "partial" if failed else "ok"
    return PortfolioSubmit(
        portfolio_id,
        status,
        sent=sent,
        failed=failed,
        held=held + len(gapped),
        gap_held=tuple(t.id for t in gapped),
        report_id=report_id,
    )


def _gate_reason(gate: CheckResult) -> str:
    report = gate.report
    unknown = [i.key for i in report.items if i.kind == "unresolved_order"]
    if unknown:
        names = ", ".join(unknown[:5])
        return f"submit check {report.id}: order(s) still unknown until reconciled: {names}"
    kinds = ", ".join(sorted({i.kind for i in report.items if i.material}))
    what = f": {kinds}" if kinds else ""
    detail = f" ({report.detail})" if report.detail else ""
    return f"submit check {report.id} is {report.status}{what}{detail}"


def _price_band(risk: RiskPolicy | None, owner: Any) -> Any:
    """The portfolio's ``price_band`` settings: the global policy tightened
    by its owner's limits and its own, as the tick merges them."""
    from stonks.accounts.book import tighter_of
    from stonks.config import RiskPolicy
    from stonks.production.rules._common import settings_of

    base = risk if risk is not None else RiskPolicy()
    overrides: list[Mapping[str, Any]] = []
    if owner is not None:
        for key in ("owner_risk_json", "risk_policy_json"):
            raw = owner[key]
            if raw:
                overrides.append(json.loads(raw))
    return settings_of(tighter_of(base, *overrides), "price_band")


def _preopen_quotes(
    broker: Broker, tickets: Sequence[Ticket], limit: float | None, log: Any
) -> Mapping[str, Quote]:
    """The latest quotes of the opening tickets. Empty while the check is
    off, or when the broker has none (an opening ticket is then held)."""
    if limit is None or not isinstance(broker, QuoteSource):
        return {}
    tickers = sorted({t.ticker for t in tickets if is_opening(t.order)})
    if not tickers:
        return {}
    try:
        return broker.quotes(tickers)
    except Exception as exc:
        log.warning("submit.quotes_failed", error=str(exc))
        return {}


def _alert_gaps(
    state: SqliteState,
    portfolio_id: str,
    tickets: Sequence[Ticket],
    reasons: Mapping[str, str],
    clock: Clock,
    publish: Publish | None,
) -> None:
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    event = Event(
        category="risk",
        level="warning",
        urgency="high",
        title="Orders held before the open",
        body=f"{len(tickets)} opening order(s) held at submit: "
        f"{reasons[tickets[0].client_id]}. They are not sent today. "
        "The next tick decides afresh.",
        audience=Audience.owner_of(portfolio_id),
        dedupe_key=f"preopen_gap:{portfolio_id}:{today(clock).isoformat()}",
        deep_link="/approvals",
        portfolio_id=portfolio_id,
    )
    try:
        (publish or configured_router(state).publish)(event)
    except Exception as exc:
        _log.error("submit.notify_failed", error=str(exc))


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
