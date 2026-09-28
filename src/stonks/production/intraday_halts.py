"""Halts for the intraday loop (roadmap 21.3.2, P40, P41).

A daily tick reads the halts once through the ``risk_halts`` trade gate.
An intraday book must see them on every event, so a kill switch pressed at
10:31 stops the next order, not tomorrow's. The event driver calls:

- :func:`event_verdict` on every event: the halts in force for the
  portfolio (global, its owner's, its own and its parent's) and the
  strictest mode. ``cancel_working`` says a stop-all kill switch is in
  force, so the driver also cancels working orders at the broker;
- :func:`gate_event_orders` on the orders the rules let through: ``buys``
  drops opening orders and keeps closes, ``all`` places nothing;
- :func:`trip_intraday_loss` after each mark: opens (or escalates) the
  portfolio's ``intraday_loss`` halt when the loss limit is breached;
- :func:`trip_intraday_runaway` after the rules ran: opens the portfolio's
  ``runaway`` halt when the order rate cap dropped opening orders.

A trip is written once. While it is open, later breaches return the same
halt. Clearing it needs a person and a reason (``clear_halt``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from stonks.core.types import Order
from stonks.logging import get_logger
from stonks.production.halts import (
    Halt,
    HaltMode,
    active_halts,
    escalate_halt,
    halts_enabled,
    notify_trip,
    trip_halt,
)
from stonks.production.rules import RiskAdjustment, RiskContext
from stonks.production.rules._common import is_opening
from stonks.production.rules.intraday_loss import loss_breach
from stonks.store.state import SqliteState

__all__ = [
    "ACTOR",
    "EventVerdict",
    "event_verdict",
    "gate_event_orders",
    "trip_intraday_loss",
    "trip_intraday_runaway",
]

_log = get_logger("stonks.production.intraday_halts")

ACTOR = "system"
#: The adjustment tag of the order rate cap (``rules.intraday_orders``).
ORDER_RATE = "intraday_order_rate"

Publish = Callable[[Any], Any]


@dataclass(frozen=True)
class EventVerdict:
    """The halts in force at one event. ``mode`` is ``None`` when trading
    may go on, else the strictest mode of the open halts."""

    mode: HaltMode | None = None
    reasons: tuple[str, ...] = ()
    halt_ids: tuple[int, ...] = ()
    #: A stop-all kill switch is in force: cancel working orders too.
    cancel_working: bool = False


def _owner(state: SqliteState, portfolio_id: str) -> str | None:
    rows = state.sql("SELECT owner_id FROM portfolios WHERE id = ?", [portfolio_id])
    return rows[0]["owner_id"] if rows else None


def event_verdict(
    state: SqliteState,
    portfolio_id: str,
    *,
    now: datetime,
    owner_id: str | None = None,
    parent_portfolio_id: str | None = None,
) -> EventVerdict:
    """The halts in force for ``portfolio_id`` at ``now``. Cheap enough to
    run on every event: two indexed reads. Without the halt table (an old
    state file) nothing is in force."""
    if not halts_enabled(state):
        return EventVerdict()
    day = now.date()
    owner = owner_id or _owner(state, portfolio_id)
    halts = active_halts(state, day, portfolio_id=portfolio_id, user_id=owner)
    if parent_portfolio_id is not None:
        seen = {h.id for h in halts}
        halts += [
            h
            for h in active_halts(state, day, portfolio_id=parent_portfolio_id)
            if h.id not in seen
        ]
    if not halts:
        return EventVerdict()
    mode: HaltMode = "all" if any(h.halt == "all" for h in halts) else "buys"
    return EventVerdict(
        mode=mode,
        reasons=tuple(f"{h.kind} ({h.target}): {h.reason}" for h in halts),
        halt_ids=tuple(h.id for h in halts),
        cancel_working=any(h.kind == "kill" and h.halt == "all" for h in halts),
    )


def gate_event_orders(
    orders: Sequence[Order], verdict: EventVerdict, positions: Mapping[str, float]
) -> tuple[list[Order], list[Order]]:
    """``(kept, blocked)``. ``buys`` blocks orders that open or grow a
    position (buys of longs and short sales) and keeps every close;
    ``all`` blocks everything, the kill switch's stop-all."""
    if verdict.mode is None:
        return list(orders), []
    if verdict.mode == "all":
        return [], list(orders)
    kept = [o for o in orders if not is_opening(o, positions)]
    blocked = [o for o in orders if is_opening(o, positions)]
    return kept, blocked


def _notify(state: SqliteState, halt: Halt, notify: bool, publish: Publish | None) -> None:
    if notify:
        notify_trip(state, halt, publish)


def trip_intraday_loss(
    state: SqliteState,
    portfolio_id: str,
    ctx: RiskContext,
    *,
    notify: bool = True,
    publish: Publish | None = None,
) -> Halt | None:
    """Open the portfolio's ``intraday_loss`` halt when ``ctx`` breaches
    the loss limit (``rules.intraday_loss.loss_breach``). A hard breach
    turns an open ``buys`` halt into ``all`` (never back). Returns the
    halt in force, or ``None`` when there is no breach."""
    breach = loss_breach(ctx)
    if breach is None or ctx.intraday is None:
        return None
    day: date = ctx.intraday.now.date()
    halt, created = trip_halt(
        state,
        "intraday_loss",
        reason=breach.reason,
        actor=ACTOR,
        portfolio_id=portfolio_id,
        halt=breach.halt,
        on=day,
    )
    if not created and breach.halt == "all" and halt.halt == "buys":
        halt = escalate_halt(state, halt.id, actor=ACTOR, reason=breach.reason)
        created = True
    if created:
        _log.warning(
            "intraday.loss_halt",
            halt_id=halt.id,
            portfolio_id=portfolio_id,
            level=breach.level,
            halt=halt.halt,
            loss=breach.loss,
        )
        _notify(state, halt, notify, publish)
    return halt


def trip_intraday_runaway(
    state: SqliteState,
    portfolio_id: str,
    adjustments: Iterable[RiskAdjustment],
    *,
    on: date,
    notify: bool = True,
    publish: Publish | None = None,
) -> Halt | None:
    """Open the portfolio's ``runaway`` halt (buys) when the order rate
    cap dropped an opening order. Returns the halt in force, else ``None``."""
    reason = next((a.reason for a in adjustments if a.rule == ORDER_RATE), None)
    if reason is None:
        return None
    halt, created = trip_halt(
        state,
        "runaway",
        reason=f"intraday order burst: {reason}",
        actor=ACTOR,
        portfolio_id=portfolio_id,
        on=on,
    )
    if created:
        _log.warning("intraday.runaway_halt", halt_id=halt.id, portfolio_id=portfolio_id)
        _notify(state, halt, notify, publish)
    return halt
