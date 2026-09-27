"""The owner's allocation per live portfolio (roadmap 19.6).

The owner sets, by hand, how much Stonks may trade in a live portfolio.
The ``capital_ramp`` rule caps the book's gross exposure at it. There are
no automatic steps or suggestions: a bad week raises an alert but never
changes the amount.

This module keeps the rows consistent and writes the audit row. Who may
change it (the portfolio's owner, with a fresh second factor) is checked
by the service layer (``stonks.app.live``) before it calls
:func:`set_allocation`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from stonks.accounts.audit import AuditLog, iso_now
from stonks.store.state import SqliteState

TABLE = "live_allocations"


class AllocationError(ValueError):
    """A request that breaks a rule (negative amount, missing reason, ...)."""


@dataclass(frozen=True)
class Allocation:
    portfolio_id: str
    amount: float
    currency: str
    reason: str
    updated_at: str
    updated_by: str


def allocations_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def get_allocation(state: SqliteState, portfolio_id: str) -> Allocation | None:
    """The portfolio's allocation, or ``None`` when none was ever set."""
    if not allocations_enabled(state):
        return None
    rows = state.sql(f"SELECT * FROM {TABLE} WHERE portfolio_id = ?", [portfolio_id])
    if not rows:
        return None
    r = rows[0]
    return Allocation(
        portfolio_id=r["portfolio_id"],
        amount=float(r["amount"]),
        currency=r["currency"],
        reason=r["reason"],
        updated_at=r["updated_at"],
        updated_by=r["updated_by"],
    )


def set_allocation(
    state: SqliteState,
    portfolio_id: str,
    amount: float,
    *,
    currency: str,
    actor: str,
    reason: str,
) -> Allocation:
    """Set the allocation and write an ``audit_log`` row with the old and
    new amounts, in one transaction."""
    if not isinstance(amount, int | float) or not math.isfinite(amount) or amount < 0:
        raise AllocationError(f"the allocation must be a number of at least 0, got {amount!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise AllocationError("a reason is required")
    if not isinstance(actor, str) or not actor.strip():
        raise AllocationError("an actor is required")
    code = (currency or "").strip().upper()
    if len(code) != 3:
        raise AllocationError(f"a three-letter currency is required, got {currency!r}")
    now = iso_now()
    with state.transaction():
        old = get_allocation(state, portfolio_id)
        state.execute(
            f"INSERT INTO {TABLE} (portfolio_id, amount, currency, reason, updated_at,"
            " updated_by) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (portfolio_id) DO UPDATE SET amount = excluded.amount,"
            " currency = excluded.currency, reason = excluded.reason,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            [portfolio_id, float(amount), code, reason.strip(), now, actor.strip()],
        )
        AuditLog(state).record(
            actor,
            "live.allocation_set",
            "portfolio",
            portfolio_id,
            portfolio_id=portfolio_id,
            details={
                "amount": float(amount),
                "currency": code,
                "previous": None if old is None else old.amount,
                "reason": reason.strip(),
            },
        )
        new = get_allocation(state, portfolio_id)
    assert new is not None
    return new
