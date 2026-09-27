"""Order tickets (roadmap 19.8, design ``docs/design/live-trading.md``
section 4 "Approve mode").

A live book decides after the close and writes one ticket per order
instead of sending it. The ``live_submit`` job sends approved tickets in
the submit window before the next open (``production.submit``).

```
awaiting_approval --approve (step-up)--> approved --submit--> submitted --> filled
        |                                    |                    |      --> unfilled
        +--reject (reason)--> rejected       +--> expired         |      --> cancelled
        +--deadline---------> expired        +--> failed          +------> failed
```

- **Auto** books write tickets already ``approved`` by ``service:system``,
  so the submit path is the same for both modes and the table is the audit
  trail of what was sent and why.
- **Held** tickets wait for a person: every order of an ``approve``
  subscription (``hold = approve_mode``), and every close of a runaway run,
  even in auto (``hold = runaway``).
- A ticket carries its order's client id, so it becomes that order at the
  broker and is idempotent like any order. One ticket per client id: a
  same-day re-run of the tick leaves the first ticket as it is.
- A ticket not sent by its deadline expires. Yesterday's decisions are
  never sent late: the next tick decides afresh.
- Every change goes through :data:`TRANSITIONS`. Decisions write an
  ``audit_log`` row in the same transaction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any, Literal, get_args

from stonks.accounts.audit import AuditLog
from stonks.core.types import Order
from stonks.logging import get_logger
from stonks.production.live.settings import SubmitSettings
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.tickets")

TicketStatus = Literal[
    "awaiting_approval",
    "approved",
    "rejected",
    "expired",
    "submitted",
    "filled",
    "unfilled",
    "cancelled",
    "failed",
]
#: Why a ticket waits for a person.
Hold = Literal["approve_mode", "runaway"]

TICKET_STATUSES: tuple[str, ...] = get_args(TicketStatus)
#: Tickets nothing has been sent for yet.
OPEN_STATUSES: frozenset[str] = frozenset({"awaiting_approval", "approved"})
#: The actor of an auto book's tickets.
SYSTEM_ACTOR = "service:system"

TRANSITIONS: Mapping[str, frozenset[str]] = {
    "awaiting_approval": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"submitted", "expired", "failed"}),
    "submitted": frozenset({"filled", "unfilled", "cancelled", "failed"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "filled": frozenset(),
    "unfilled": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}


class TicketRefused(ValueError):
    """A decision the ticket does not allow (decided already, expired, not
    yours, no reason for a rejection)."""


class IllegalTicketTransition(ValueError):
    """A status change :data:`TRANSITIONS` does not allow."""


class TicketNotFound(LookupError):
    """No such ticket, or not one the caller may see."""


@dataclass(frozen=True)
class SubmitWindow:
    """When a ticket may be sent: from ``submit_after`` until ``expires_at``."""

    submit_after: datetime
    expires_at: datetime


def submit_window(as_of: date, settings: SubmitSettings) -> SubmitWindow:
    """The window before the first session open after ``as_of`` (the
    decision day) on ``settings.calendar``."""
    from stonks.scheduling.calendar import get_calendar

    session = get_calendar(settings.calendar).next_session(as_of)
    return SubmitWindow(
        submit_after=session.open - timedelta(minutes=settings.window_minutes),
        expires_at=session.open - timedelta(minutes=settings.deadline_minutes),
    )


@dataclass(frozen=True)
class Ticket:
    id: str
    portfolio_id: str
    tick_id: str | None
    as_of: date
    client_id: str
    strategy_id: str | None
    ticker: str
    side: str
    quantity: float
    limit_price: float | None
    order: Order
    preview: Mapping[str, Any]
    reason: Mapping[str, Any]
    hold: Hold | None
    status: TicketStatus
    submit_after: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    submitted_at: datetime | None
    status_reason: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def awaiting_approval(self) -> bool:
        return self.status == "awaiting_approval"

    @classmethod
    def from_row(cls, row: Any) -> Ticket:
        def ts(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return cls(
            id=row["id"],
            portfolio_id=row["portfolio_id"],
            tick_id=row["tick_id"],
            as_of=date.fromisoformat(row["as_of"]),
            client_id=row["client_id"],
            strategy_id=row["strategy_id"],
            ticker=row["ticker"],
            side=row["side"],
            quantity=float(row["quantity"]),
            limit_price=row["limit_price"],
            order=order_from_json(row["order_json"]),
            preview=json.loads(row["preview_json"] or "{}"),
            reason=json.loads(row["reason_json"] or "{}"),
            hold=row["hold"],
            status=row["status"],
            submit_after=datetime.fromisoformat(row["submit_after"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            decided_by=row["decided_by"],
            decided_at=ts(row["decided_at"]),
            decision_reason=row["decision_reason"],
            submitted_at=ts(row["submitted_at"]),
            status_reason=row["status_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


# ---- the order inside a ticket ---------------------------------------------------------


def order_to_json(order: Order) -> str:
    values: dict[str, Any] = {}
    for f in fields(order):
        value = getattr(order, f.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, Mapping):
            value = dict(value)
        values[f.name] = value
    return json.dumps(values, sort_keys=True, default=str)


def order_from_json(text: str) -> Order:
    values = json.loads(text)
    if values.get("decided_at"):
        values["decided_at"] = datetime.fromisoformat(values["decided_at"])
    known = {f.name for f in fields(Order)}
    return Order(**{k: v for k, v in values.items() if k in known})


def _preview(order: Order) -> dict[str, Any]:
    """The reference price and notional the person approves."""
    price = order.limit_price or order.decision_price
    preview: dict[str, Any] = {"reference_price": order.decision_price}
    if price is not None:
        preview["notional"] = round(order.quantity * float(price), 6)
    return preview


def _reason(order: Order) -> dict[str, Any]:
    """Why the book wants the order: the strategy and its decision context
    (signal score, rank, target weight, trigger)."""
    reason: dict[str, Any] = dict(order.decision_context or {})
    reason["strategy_id"] = order.strategy_id
    if order.position_effect is not None:
        reason["position_effect"] = order.position_effect
    return reason


# ---- writes ------------------------------------------------------------------------


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def write_tickets(
    state: SqliteState,
    orders: Sequence[Order],
    *,
    portfolio_id: str,
    tick_id: str | None,
    as_of: date,
    window: SubmitWindow,
    hold: Callable[[Order], Hold | None],
    now: datetime,
    rules: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    previews: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Ticket]:
    """One ticket per order. ``hold(order)`` names why it waits for a
    person, or ``None`` (approved by the system). ``rules``: the risk
    adjustments that touched each order, by client id. ``previews``: extra
    preview fields (a what-if answer) by client id. An order whose client
    id has a ticket already is left out. Returns the tickets written."""
    written: list[str] = []
    stamp = _iso(now)
    with state.transaction():
        for order in orders:
            reason_for = hold(order)
            status: TicketStatus = "awaiting_approval" if reason_for else "approved"
            reason = _reason(order)
            touched = list((rules or {}).get(order.client_id, ()))
            if touched:
                reason["rules"] = [dict(r) for r in touched]
            preview = _preview(order) | dict((previews or {}).get(order.client_id, {}))
            ticket_id = f"tkt_{uuid.uuid4().hex[:16]}"
            cur = state.execute(
                "INSERT INTO order_tickets (id, portfolio_id, tick_id, as_of, client_id,"
                " strategy_id, ticker, side, quantity, limit_price, order_json, preview_json,"
                " reason_json, hold, status, submit_after, expires_at, decided_by, decided_at,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (client_id) DO NOTHING",
                [
                    ticket_id,
                    portfolio_id,
                    tick_id,
                    as_of.isoformat(),
                    order.client_id,
                    order.strategy_id,
                    order.ticker,
                    order.side,
                    order.quantity,
                    order.limit_price,
                    order_to_json(order),
                    json.dumps(preview, sort_keys=True, default=str),
                    json.dumps(reason, sort_keys=True, default=str),
                    reason_for,
                    status,
                    _iso(window.submit_after),
                    _iso(window.expires_at),
                    None if reason_for else SYSTEM_ACTOR,
                    None if reason_for else stamp,
                    stamp,
                    stamp,
                ],
            )
            if cur.rowcount:
                written.append(ticket_id)
    return [get_ticket(state, tid) for tid in written]


def set_ticket_status(
    state: SqliteState,
    ticket_id: str,
    new: TicketStatus,
    *,
    now: datetime,
    reason: str | None = None,
) -> Ticket:
    """Move a ticket through :data:`TRANSITIONS` (the same status again is a
    no-op). ``submitted`` also stamps ``submitted_at``."""
    ticket = get_ticket(state, ticket_id)
    if ticket.status == new:
        return ticket
    if new not in TRANSITIONS[ticket.status]:
        raise IllegalTicketTransition(f"a ticket cannot go from {ticket.status} to {new}")
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [new, _iso(now)]
    if new == "submitted":
        sets.append("submitted_at = ?")
        params.append(_iso(now))
    if reason is not None:
        sets.append("status_reason = ?")
        params.append(reason[:500])
    # the WHERE guard makes a concurrent change lose instead of overwrite
    cur = state.execute(
        f"UPDATE order_tickets SET {', '.join(sets)} WHERE id = ? AND status = ?",
        [*params, ticket_id, ticket.status],
    )
    if not cur.rowcount:
        raise IllegalTicketTransition(f"ticket {ticket_id} changed while it was updated")
    return get_ticket(state, ticket_id)


def decide_tickets(
    state: SqliteState,
    ticket_ids: Sequence[str],
    *,
    approve: bool,
    actor: str,
    now: datetime,
    reason: str | None = None,
    portfolio_ids: Sequence[str] | None = None,
) -> list[Ticket]:
    """Approve or reject tickets that wait for a person, all or none.
    ``portfolio_ids``: the portfolios the actor may decide for (``None``:
    any, for the service layer's own checks). A rejection needs a reason.
    A ticket past its deadline is refused (and expires)."""
    ids = list(dict.fromkeys(ticket_ids))
    if not ids:
        raise TicketRefused("no tickets to decide")
    note = (reason or "").strip()
    if not approve and not note:
        raise TicketRefused("a rejection needs a reason")
    tickets = [_find(state, tid, portfolio_ids) for tid in ids]
    for ticket in tickets:
        if ticket.status != "awaiting_approval":
            raise TicketRefused(f"ticket {ticket.id} is {ticket.status}, not awaiting approval")
    late = [t.id for t in tickets if now >= t.expires_at]
    if late:
        expire_due(state, now)
        raise TicketRefused(f"ticket {late[0]} expired at its submit deadline")
    audit = AuditLog(state)
    stamp = _iso(now)
    with state.transaction():
        for ticket in tickets:
            cur = state.execute(
                "UPDATE order_tickets SET status = ?, decided_by = ?, decided_at = ?,"
                " decision_reason = ?, updated_at = ? WHERE id = ? AND status = 'awaiting_approval'",
                [
                    "approved" if approve else "rejected",
                    actor,
                    stamp,
                    note or None,
                    stamp,
                    ticket.id,
                ],
            )
            if not cur.rowcount:  # decided by someone else meanwhile: the batch rolls back
                raise TicketRefused(f"ticket {ticket.id} was decided meanwhile")
            audit.record(
                actor,
                "ticket.approve" if approve else "ticket.reject",
                "order_ticket",
                ticket.id,
                portfolio_id=ticket.portfolio_id,
                details={"client_id": ticket.client_id, **({"reason": note} if note else {})},
            )
    return [get_ticket(state, tid) for tid in ids]


def expire_due(state: SqliteState, now: datetime) -> list[str]:
    """Expire every open ticket whose deadline passed. Returns their ids."""
    rows = state.sql(
        "SELECT id FROM order_tickets WHERE status IN ('awaiting_approval', 'approved')"
        " AND expires_at <= ? ORDER BY id",
        [_iso(now)],
    )
    ids = [r["id"] for r in rows]
    if ids:
        marks = ",".join("?" for _ in ids)
        state.execute(
            "UPDATE order_tickets SET status = 'expired', updated_at = ?,"
            " status_reason = COALESCE(status_reason, 'not sent by the submit deadline')"
            f" WHERE id IN ({marks}) AND status IN ('awaiting_approval', 'approved')",
            [_iso(now), *ids],
        )
        _log.info("tickets.expired", count=len(ids))
    return ids


#: The ticket status an order's fine state ends it in.
_FROM_ORDER: Mapping[str, TicketStatus] = {
    "filled": "filled",
    "expired": "unfilled",
    "cancelled": "cancelled",
    "rejected": "failed",
}


def sync_submitted(state: SqliteState, *, now: datetime) -> int:
    """Move submitted tickets to their order's outcome: filled, unfilled
    (the auction did not fill), cancelled or failed (the broker refused
    it). An order that ended with some fills counts as filled. Returns how
    many tickets changed."""
    has_state = any(
        r["name"] == "state" for r in state.sql("SELECT name FROM pragma_table_info('orders')")
    )
    fine = "o.state" if has_state else "NULL"
    rows = state.sql(
        f"SELECT t.id, o.status, {fine} AS state, o.status_reason,"
        " (SELECT COALESCE(SUM(f.quantity), 0) FROM fills f WHERE f.order_client_id = o.client_id)"
        " AS filled"
        " FROM order_tickets t JOIN orders o ON o.client_id = t.client_id"
        " WHERE t.status = 'submitted'"
    )
    changed = 0
    for row in rows:
        final = row["state"] or row["status"]
        outcome = _FROM_ORDER.get(final)
        if outcome is None:
            continue
        note = row["status_reason"]
        if outcome in ("unfilled", "cancelled") and float(row["filled"]) > 0:
            outcome, note = "filled", f"partly filled ({float(row['filled']):g})"
        set_ticket_status(state, row["id"], outcome, now=now, reason=note)
        changed += 1
    return changed


# ---- reads -------------------------------------------------------------------------


def get_ticket(state: SqliteState, ticket_id: str) -> Ticket:
    rows = state.sql("SELECT * FROM order_tickets WHERE id = ?", [ticket_id])
    if not rows:
        raise TicketNotFound(f"ticket {ticket_id!r} not found")
    return Ticket.from_row(rows[0])


def _find(state: SqliteState, ticket_id: str, portfolio_ids: Sequence[str] | None) -> Ticket:
    ticket = get_ticket(state, ticket_id)
    if portfolio_ids is not None and ticket.portfolio_id not in portfolio_ids:
        raise TicketNotFound(f"ticket {ticket_id!r} not found")
    return ticket


def list_tickets(
    state: SqliteState,
    *,
    portfolio_ids: Sequence[str] | None = None,
    status: str | None = None,
    tick_id: str | None = None,
    limit: int | None = None,
) -> list[Ticket]:
    """Tickets, newest first (``portfolio_ids`` ``None``: every portfolio)."""
    where: list[str] = []
    params: list[Any] = []
    if portfolio_ids is not None:
        if not portfolio_ids:
            return []
        where.append(f"portfolio_id IN ({','.join('?' for _ in portfolio_ids)})")
        params += list(portfolio_ids)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if tick_id is not None:
        where.append("tick_id = ?")
        params.append(tick_id)
    sql = "SELECT * FROM order_tickets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, ticker, id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [Ticket.from_row(r) for r in state.sql(sql, params)]


def due_tickets(
    state: SqliteState, now: datetime, *, portfolio_id: str | None = None
) -> list[Ticket]:
    """Approved tickets inside their submit window: sells first (their
    proceeds), then buys, each in ticker order."""
    sql = (
        "SELECT * FROM order_tickets WHERE status = 'approved'"
        " AND submit_after <= ? AND expires_at > ?"
    )
    params: list[Any] = [_iso(now), _iso(now)]
    if portfolio_id is not None:
        sql += " AND portfolio_id = ?"
        params.append(portfolio_id)
    sql += " ORDER BY portfolio_id, CASE side WHEN 'sell' THEN 0 ELSE 1 END, ticker, id"
    return [Ticket.from_row(r) for r in state.sql(sql, params)]


def awaiting_counts(state: SqliteState, *, owner_id: str | None = None) -> dict[str, int]:
    """Tickets waiting for a person, per portfolio (of ``owner_id``)."""
    sql = (
        "SELECT t.portfolio_id, COUNT(*) AS n FROM order_tickets t"
        " JOIN portfolios p ON p.id = t.portfolio_id WHERE t.status = 'awaiting_approval'"
    )
    params: list[Any] = []
    if owner_id is not None:
        sql += " AND p.owner_id = ?"
        params.append(owner_id)
    rows = state.sql(sql + " GROUP BY t.portfolio_id", params)
    return {r["portfolio_id"]: int(r["n"]) for r in rows}


def tickets_recorded(state: SqliteState) -> bool:
    """Whether the state DB has the ticket table (migration 029)."""
    return bool(
        state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'order_tickets'")
    )
