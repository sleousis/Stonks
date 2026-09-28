"""The options approval level of a portfolio (roadmap 17.8).

Brokers grant options trading in levels. Stonks keeps its own level per
portfolio, set by the owner with a fresh second factor and a reason, and
never above what the broker granted (the broker refuses the rest anyway):

| Level | May open |
|---|---|
| ``none`` | nothing (the default) |
| ``covered`` | covered calls, cash-secured puts, long calls and puts, protective puts |
| ``spreads`` | plus verticals and iron condors |
| ``naked`` | plus uncovered short puts. Naked short calls stay refused. |

The level caps the ``short_option_guard`` rule's ``approval_level`` (it
can only tighten it). Who may change it (the owner, step-up) is checked by
the service layer before :func:`set_approval` runs. Every change writes an
``audit_log`` row in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from stonks.accounts.audit import AuditLog
from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now
from stonks.store.state import SqliteState

ApprovalLevel = Literal["none", "covered", "spreads", "naked"]
LEVELS: tuple[ApprovalLevel, ...] = get_args(ApprovalLevel)
DEFAULT_LEVEL: ApprovalLevel = "none"

#: Our level -> the ``short_option_guard`` level (1 to 4). ``none`` has no
#: guard level: nothing opens.
GUARD_LEVEL: dict[ApprovalLevel, int] = {"covered": 2, "spreads": 3, "naked": 4}

TABLE = "option_approvals"


class ApprovalError(ValueError):
    """A change that breaks a rule (unknown level, no reason, no actor)."""


@dataclass(frozen=True)
class Approval:
    portfolio_id: str
    level: ApprovalLevel
    reason: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


def approvals_enabled(state: SqliteState) -> bool:
    """The state DB carries the table (migration 046)."""
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def get_approval(state: SqliteState, portfolio_id: str) -> Approval:
    """The portfolio's level. ``none`` when never set (or before 046)."""
    if not approvals_enabled(state):
        return Approval(portfolio_id, DEFAULT_LEVEL)
    rows = state.sql(f"SELECT * FROM {TABLE} WHERE portfolio_id = ?", [portfolio_id])
    if not rows:
        return Approval(portfolio_id, DEFAULT_LEVEL)
    r = rows[0]
    return Approval(
        portfolio_id=r["portfolio_id"],
        level=r["level"],
        reason=r["reason"],
        updated_at=r["updated_at"],
        updated_by=r["updated_by"],
    )


def set_approval(
    state: SqliteState,
    portfolio_id: str,
    level: str,
    *,
    actor: str,
    reason: str,
    clock: Clock = SYSTEM_CLOCK,
) -> Approval:
    """Set the level and write the audit row, in one transaction."""
    if level not in LEVELS:
        raise ApprovalError(f"unknown options approval level {level!r}; use {', '.join(LEVELS)}")
    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalError("a reason is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ApprovalError("an actor is required")
    if not approvals_enabled(state):
        raise ApprovalError("the state DB has no option_approvals table: run stonks db init")
    now = iso_now(clock)
    with state.transaction():
        old = get_approval(state, portfolio_id)
        state.execute(
            f"INSERT INTO {TABLE} (portfolio_id, level, reason, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (portfolio_id) DO UPDATE SET"
            " level = excluded.level, reason = excluded.reason,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            [portfolio_id, level, reason.strip(), now, actor.strip()],
        )
        AuditLog(state).record(
            actor,
            "options.approval_set",
            "portfolio",
            portfolio_id,
            portfolio_id=portfolio_id,
            details={"level": level, "previous": old.level, "reason": reason.strip()},
        )
    return get_approval(state, portfolio_id)


def guard_level(level: ApprovalLevel) -> int | None:
    """The ``short_option_guard`` level ``level`` allows (``None``: none)."""
    return GUARD_LEVEL.get(level)
