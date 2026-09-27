"""The order state machine (roadmap 19.1).

An order at a live broker moves through more states than the ledger's
five statuses. The fine state lives in ``orders.state`` (migration 027)
and the coarse ``orders.status`` follows it (:func:`ledger_status`), so
every existing reader keeps working.

```
pending --submit--> submitted --> accepted --> partially_filled --> filled
   |                    |            |               |
   |                    +------------+---------------+--> pending_cancel --> cancelled
   |                    |            |               |                        expired
   +--> rejected        +--> rejected, cancelled, expired
any open state --timeout--> unknown --reconciliation--> the broker's state
```

Rules:

- Every change goes through :data:`TRANSITIONS` (:func:`check_transition`).
  A terminal state (``filled``, ``cancelled``, ``expired``, ``rejected``)
  never changes again. Writing the same state again is a no-op.
- **Timed-out orders.** A submit, cancel or modify whose outcome is not
  known becomes ``unknown`` (:func:`mark_unknown`). Nothing is sent for it
  again until reconciliation resolves it by client id (the broker's order
  reference). Only ``pending`` may be submitted (:func:`may_submit`).
- **Startup reconciliation.** :func:`require_reconciled` refuses to open a
  submit window while any order of the portfolio is ``unknown``.

Rows written before migration 027 (or by writers not yet moved over) have
no ``state``: it is read from their status (:func:`state_from_status`).
Vendor status strings are mapped by each adapter (``brokers/ibkr/status.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now
from stonks.core.types import OrderStatus
from stonks.execution.brokers.base import ORDER_STATES, OrderState
from stonks.store.state import SqliteState

__all__ = [
    "ORDER_STATES",
    "TERMINAL",
    "TRANSITIONS",
    "IllegalTransitionError",
    "OrderState",
    "ReconciliationPendingError",
    "can_transition",
    "check_transition",
    "current_state",
    "ledger_status",
    "mark_unknown",
    "may_submit",
    "require_reconciled",
    "state_from_status",
    "unknown_orders",
    "write_state",
]
TERMINAL: frozenset[OrderState] = frozenset({"filled", "cancelled", "expired", "rejected"})
#: States a broker may report for an order it knows (what ``unknown``
#: resolves to).
_KNOWN: frozenset[OrderState] = frozenset(ORDER_STATES) - {"pending", "unknown"}

#: Every legal change (a state to itself is always a no-op).
TRANSITIONS: Mapping[OrderState, frozenset[OrderState]] = {
    # written before the submit: the broker may answer with any state
    "pending": frozenset(
        {"submitted", "accepted", "partially_filled", "filled", "rejected", "cancelled", "unknown"}
    ),
    "submitted": frozenset(
        {
            "accepted",
            "partially_filled",
            "filled",
            "pending_cancel",
            "cancelled",
            "expired",
            "rejected",
            "unknown",
        }
    ),
    "accepted": frozenset(
        {
            "partially_filled",
            "filled",
            "pending_cancel",
            "cancelled",
            "expired",
            "rejected",
            "unknown",
        }
    ),
    "partially_filled": frozenset({"filled", "pending_cancel", "cancelled", "expired", "unknown"}),
    # a cancel can lose the race to a fill, or be refused (back to accepted)
    "pending_cancel": frozenset(
        {"cancelled", "expired", "filled", "partially_filled", "accepted", "unknown"}
    ),
    # only reconciliation leaves unknown, to what the broker reports
    "unknown": _KNOWN,
    "filled": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
    "rejected": frozenset(),
}

_LEDGER: Mapping[OrderState, OrderStatus] = {
    "pending": "pending",
    "submitted": "pending",
    "accepted": "pending",
    "pending_cancel": "pending",
    "unknown": "pending",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "cancelled": "cancelled",
    "expired": "cancelled",
    "rejected": "rejected",
}


class IllegalTransitionError(ValueError):
    """A change the state machine does not allow."""


class ReconciliationPendingError(RuntimeError):
    """Orders are still ``unknown``: no submit until reconciliation resolves them."""

    def __init__(self, client_ids: tuple[str, ...]) -> None:
        self.client_ids = client_ids
        super().__init__(
            f"{len(client_ids)} order(s) in an unknown state; reconcile before submitting: "
            + ", ".join(client_ids[:10])
        )


def is_order_state(value: object) -> bool:
    return isinstance(value, str) and value in ORDER_STATES


def can_transition(current: OrderState, new: OrderState) -> bool:
    return current == new or new in TRANSITIONS[current]


def check_transition(current: OrderState, new: OrderState) -> None:
    if not can_transition(current, new):
        raise IllegalTransitionError(f"an order cannot go from {current} to {new}")


def ledger_status(state: OrderState) -> OrderStatus:
    """The coarse ``orders.status`` for a fine state."""
    return _LEDGER[state]


def state_from_status(status: str) -> OrderState:
    """The fine state of a row with no ``state`` (older writers)."""
    if not is_order_state(status):
        raise ValueError(f"unknown order status {status!r}")
    return cast(OrderState, status)


def may_submit(current: OrderState | None) -> bool:
    """Only a ``pending`` row (written, never sent) may be submitted.
    ``unknown`` waits for reconciliation."""
    return current == "pending"


def _has_state(db: SqliteState) -> bool:
    return any(r["name"] == "state" for r in db.sql("SELECT name FROM pragma_table_info('orders')"))


def current_state(db: SqliteState, client_id: str) -> OrderState | None:
    """The order's state, or ``None`` when there is no such order."""
    cols = "status, state" if _has_state(db) else "status"
    rows = db.sql(f"SELECT {cols} FROM orders WHERE client_id = ?", [client_id])
    if not rows:
        return None
    row: Any = rows[0]
    if "state" in row.keys() and row["state"]:  # noqa: SIM118 - sqlite3.Row
        return cast(OrderState, row["state"])
    return state_from_status(str(row["status"]))


def write_state(
    db: SqliteState,
    client_id: str,
    new: OrderState,
    *,
    reason: str | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> bool:
    """Move ``client_id`` to ``new`` through the transition table. Writes
    ``state``, the matching ``status`` and ``updated_at`` (and
    ``status_reason`` when given). Returns whether anything changed.
    Raises :class:`IllegalTransitionError` or ``KeyError`` (no such order)."""
    current = current_state(db, client_id)
    if current is None:
        raise KeyError(f"no order {client_id!r}")
    check_transition(current, new)
    if current == new and reason is None:
        return False
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [ledger_status(new), iso_now(clock)]
    if _has_state(db):
        sets.append("state = ?")
        params.append(new)
    if reason is not None:
        sets.append("status_reason = ?")
        params.append(reason)
    cur = db.execute(
        f"UPDATE orders SET {', '.join(sets)} WHERE client_id = ?", [*params, client_id]
    )
    return cur.rowcount > 0 and current != new


def mark_unknown(
    db: SqliteState, client_id: str, reason: str, *, clock: Clock = SYSTEM_CLOCK
) -> bool:
    """A submit, cancel or modify timed out: the outcome is unknown until
    reconciliation. A terminal order stays as it is (returns ``False``)."""
    current = current_state(db, client_id)
    if current is None or current in TERMINAL:
        return False
    return write_state(db, client_id, "unknown", reason=reason, clock=clock)


def unknown_orders(db: SqliteState, portfolio_id: str | None = None) -> tuple[str, ...]:
    """Client ids in the ``unknown`` state (of ``portfolio_id`` when given)."""
    if not _has_state(db):
        return ()
    sql = "SELECT client_id FROM orders WHERE state = 'unknown'"
    params: list[Any] = []
    if portfolio_id is not None:
        sql += " AND portfolio_id = ?"
        params.append(portfolio_id)
    return tuple(r["client_id"] for r in db.sql(sql + " ORDER BY created_at, client_id", params))


def require_reconciled(db: SqliteState, portfolio_id: str | None = None) -> None:
    """Raise :class:`ReconciliationPendingError` while any order is
    ``unknown``: the submit window stays shut until reconciliation passes."""
    pending = unknown_orders(db, portfolio_id)
    if pending:
        raise ReconciliationPendingError(pending)
