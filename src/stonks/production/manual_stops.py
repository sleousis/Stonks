"""The protective stop a person attaches to a manual entry (roadmap 23.4).

The ticket's stop price is kept on the entry order
(``decision_context.stop_price``). In a book at a broker, once the entry
has filled, one good till cancelled ``stop`` order for the filled quantity
goes to the broker through :func:`production.live.stops.send_stop_plan`:
the same state machine, reconciliation and idempotency as the strategy
stops. Its client id is the entry's plus ``:stop`` and its OCA group is the
entry's (:func:`production.live.stops.oca_group_for`), and a later manual
exit of the same ticker joins that group, so a fill of either shrinks the
other at the broker.

The stop is the person's, like the entry: it is recorded with
``origin = 'manual'`` and the strategy stop sync leaves it alone
(``load_working_stops`` skips manual rows). It is placed when the entry
order is placed (a market order that fills at once), and at every later
manual order and ``live_stops`` run of the book, so an entry that fills
later still gets it. A person may cancel it like any working order, and
it is never placed again.

A simulated book keeps the plan on the order only: its manual orders fill
at once at the latest close and there is no broker to hold a stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.types import Order, OrderSide
from stonks.logging import get_logger
from stonks.production.live.stops import (
    STOP_TRIGGER,
    StopPlan,
    oca_group_for,
    send_stop_plan,
    stop_client_id,
)
from stonks.store.state import SqliteState

__all__ = [
    "ManualStop",
    "awaiting_manual_stops",
    "manual_stop_order",
    "pending_manual_stops",
    "sync_manual_stops",
    "working_manual_stop",
]

_log = get_logger("stonks.production.manual_stops")
_EPS = 1e-9
#: An entry in one of these states has all the fills it will get.
_SETTLED = ("filled", "cancelled", "expired")


@dataclass(frozen=True)
class ManualStop:
    """A filled manual entry that carries a stop price and has no stop yet."""

    entry_client_id: str
    ticker: str
    #: The entry's side (the stop takes the other one).
    side: OrderSide
    #: What the stop covers: the entry's fills, never more than the person
    #: still holds unprotected on that side.
    filled_quantity: float
    stop_price: float
    placed_by: str | None


def _context(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def pending_manual_stops(state: SqliteState, portfolio_id: str) -> list[ManualStop]:
    """Manual entries of the book that filled, carry a stop price and never
    had a stop placed (a rejected one may be placed again)."""
    marks = ",".join("?" for _ in _SETTLED)
    rows = state.sql(
        "SELECT o.client_id, o.ticker, o.side, o.placed_by, o.decision_context_json,"
        " (SELECT COALESCE(SUM(f.quantity), 0) FROM fills f"
        "   WHERE f.order_client_id = o.client_id) AS filled"
        " FROM orders o WHERE o.portfolio_id = ? AND o.origin = 'manual'"
        f" AND o.order_type IN ('market', 'limit') AND o.status IN ({marks})"
        " AND o.decision_context_json LIKE '%stop_price%'"
        " ORDER BY o.created_at, o.client_id",
        [portfolio_id, *_SETTLED],
    )
    out: list[ManualStop] = []
    room: dict[tuple[str, str], float] | None = None
    for r in rows:
        stop = _context(r["decision_context_json"]).get("stop_price")
        if not isinstance(stop, int | float) or stop <= 0 or float(r["filled"]) <= _EPS:
            continue
        cid = str(r["client_id"])
        children = state.sql(
            "SELECT status FROM orders WHERE substr(client_id, 1, ?) = ?",
            [len(cid) + 5, f"{cid}:stop"],
        )
        if any(c["status"] != "rejected" for c in children):
            continue
        if room is None:
            room = _unprotected(state, portfolio_id)
        # never more than the person still holds of the entry's side: a
        # manual exit may have closed it while its stop waited (a stop on a
        # flat book would open the other way)
        key = (str(r["ticker"]), str(r["side"]))
        quantity = min(float(r["filled"]), room.get(key, 0.0))
        if quantity <= _EPS:
            continue
        room[key] = room.get(key, 0.0) - quantity
        out.append(
            ManualStop(
                entry_client_id=cid,
                ticker=str(r["ticker"]),
                side=r["side"],
                filled_quantity=quantity,
                stop_price=float(stop),
                placed_by=r["placed_by"],
            )
        )
    return out


def _unprotected(state: SqliteState, portfolio_id: str) -> dict[tuple[str, str], float]:
    """Per ``(ticker, entry side)``, the manual holding no working manual
    stop protects yet: the net manual fills of that side, less the working
    manual stops that close it."""
    from stonks.execution.reconcile import NON_TERMINAL_STATUSES
    from stonks.production.ownership import manual_positions

    room: dict[tuple[str, str], float] = {}
    for ticker, qty in manual_positions(state, portfolio_id).items():
        room[(ticker, "buy" if qty > 0 else "sell")] = abs(qty)
    marks = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = state.sql(
        "SELECT ticker, side, quantity FROM orders WHERE portfolio_id = ? AND origin = 'manual'"
        f" AND order_type = 'stop' AND status IN ({marks})",
        [portfolio_id, *NON_TERMINAL_STATUSES],
    )
    for r in rows:
        key = (str(r["ticker"]), "buy" if r["side"] == "sell" else "sell")
        if key in room:
            room[key] -= float(r["quantity"])
    return room


def awaiting_manual_stops(state: SqliteState, portfolio_id: str) -> bool:
    """Whether a manual entry of the book with a stop price still has no
    stop: filled already, or still working at the broker."""
    rows = state.sql(
        "SELECT o.client_id FROM orders o WHERE o.portfolio_id = ? AND o.origin = 'manual'"
        " AND o.order_type IN ('market', 'limit') AND o.status <> 'rejected'"
        " AND o.decision_context_json LIKE '%stop_price%'"
        " AND NOT EXISTS (SELECT 1 FROM orders c"
        "   WHERE substr(c.client_id, 1, length(o.client_id) + 5) = o.client_id || ':stop')"
        " LIMIT 1",
        [portfolio_id],
    )
    return bool(rows)


def manual_stop_order(
    stop: ManualStop, *, portfolio_id: str, taken: set[str], now: datetime
) -> Order:
    """The broker order that protects ``stop``'s entry."""
    return Order(
        client_id=stop_client_id(stop.entry_client_id, taken),
        ticker=stop.ticker,
        side="sell" if stop.side == "buy" else "buy",
        quantity=stop.filled_quantity,
        order_type="stop",
        stop_price=stop.stop_price,
        time_in_force="gtc",
        position_effect="close",
        portfolio_id=portfolio_id,
        oca_group=oca_group_for(portfolio_id, stop.ticker, stop.entry_client_id),
        decided_at=now,
        decision_context={
            "trigger": STOP_TRIGGER,
            "manual": True,
            "entry_client_id": stop.entry_client_id,
            "placed_by": stop.placed_by,
        },
    )


def sync_manual_stops(
    state: SqliteState,
    broker: object,
    portfolio_id: str,
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> list[str]:
    """Place the stop of every filled manual entry of the book that needs
    one. Returns the client ids placed. Never raises: a failed send is left
    for reconciliation and tried again at the next sync."""
    try:
        pending = pending_manual_stops(state, portfolio_id)
    except Exception as exc:  # pragma: no cover - a read on a broken ledger
        _log.warning("manual_stops.read_failed", portfolio_id=portfolio_id, error=str(exc))
        return []
    placed: list[str] = []
    for stop in pending:
        taken = {
            r["client_id"]
            for r in state.sql(
                "SELECT client_id FROM orders WHERE substr(client_id, 1, ?) = ?",
                [len(stop.entry_client_id) + 5, f"{stop.entry_client_id}:stop"],
            )
        }
        order = manual_stop_order(
            stop, portfolio_id=portfolio_id, taken=taken, now=datetime.now(UTC)
        )
        try:
            sync = send_stop_plan(
                state, broker, StopPlan(place=(order,)), portfolio_id=portfolio_id, clock=clock
            )
        except Exception as exc:
            _log.warning("manual_stops.send_failed", client_id=order.client_id, error=str(exc))
            continue
        state.execute(
            "UPDATE orders SET origin = 'manual', placed_by = ?, manual_reason = ?"
            " WHERE client_id = ?",
            [
                stop.placed_by,
                f"protective stop for {stop.entry_client_id}",
                order.client_id,
            ],
        )
        placed.extend(sync.placed)
    return placed


def working_manual_stop(state: SqliteState, portfolio_id: str, ticker: str) -> Any | None:
    """The newest working manual stop on ``ticker``, or ``None``."""
    from stonks.execution.reconcile import NON_TERMINAL_STATUSES

    marks = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = state.sql(
        "SELECT client_id, side, oca_group FROM orders WHERE portfolio_id = ? AND ticker = ?"
        " AND origin = 'manual' AND order_type = 'stop'"
        f" AND status IN ({marks}) ORDER BY created_at DESC LIMIT 1",
        [portfolio_id, ticker, *NON_TERMINAL_STATUSES],
    )
    return rows[0] if rows else None
