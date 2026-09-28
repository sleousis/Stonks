"""Parent orders Stonks works as child slices (roadmap 23.16).

At a broker that does not run an algo itself (``NativeAlgoBroker``), a
TWAP or VWAP ticket becomes a **parent** in ``algo_parents`` and its
planned **child slices** in ``algo_slices`` (SQLite 053). The parent never
reaches the broker. Each child is an ordinary order: client id
``<parent>.sNN``, the parent's side, type, limit and decision, a day order,
``orders.parent_client_id`` set. So the order state machine,
reconciliation, the submit gate and TCA see real orders only.

The ``algo_slices`` job (:func:`work_parents`, every few minutes) for each
working parent:

1. reconciles the children already sent;
2. sends each planned slice whose time has come, unless a halt holds it
   (a halt of new orders holds every slice, a halt of buys the slices of a
   parent that opens or grows a position) or the book stopped;
3. skips the planned slices still unsent at the window end;
4. moves the parent to the state its children add up to
   (:func:`parent_state_of`), through the same :data:`TRANSITIONS`.

```
accepted --first fill--> partially_filled --all filled--> filled
    |                         |
    +-----all slices done-----+--> expired (some or nothing filled)
    +--> rejected (every child refused)   cancelled (cancel_parent)
```

A child's own unfilled rest is not sent again: each slice is a day order
that works until it fills or the day ends.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock, today
from stonks.core.protocols import Broker
from stonks.core.types import Order
from stonks.execution.algos.base import ChildSlice, get_algo, window_of
from stonks.execution.brokers.base import (
    ExecutionSource,
    OrderCanceller,
    OrderRejectedError,
    OrderState,
    OrderStateSource,
)
from stonks.execution.order_state import TERMINAL, can_transition, mark_unknown, write_state
from stonks.execution.reconcile import reconcile_order, reconcile_orders
from stonks.logging import get_logger
from stonks.production.halts import active_halts
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.algo_slices")

_EPS = 1e-9
#: Parent states that are still working.
OPEN_PARENT_STATES: tuple[OrderState, ...] = ("accepted", "partially_filled")

SliceStatus = Literal["planned", "sent", "skipped"]


def parents_recorded(state: SqliteState) -> bool:
    """Whether the state DB has the parent tables (migration 053)."""
    return bool(
        state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'algo_parents'")
    )


def is_parent(state: SqliteState, client_id: str) -> bool:
    if not parents_recorded(state):
        return False
    return bool(state.sql("SELECT 1 FROM algo_parents WHERE client_id = ?", [client_id]))


def open_parent_count(state: SqliteState) -> int:
    if not parents_recorded(state):
        return 0
    marks = ",".join("?" for _ in OPEN_PARENT_STATES)
    rows = state.sql(
        f"SELECT COUNT(*) FROM algo_parents WHERE state IN ({marks})", list(OPEN_PARENT_STATES)
    )
    return int(rows[0][0])


def child_client_id(parent_client_id: str, seq: int) -> str:
    return f"{parent_client_id}.s{seq:02d}"


def child_order(parent: Order, piece: ChildSlice) -> Order:
    """The order of one slice: the parent's, for the slice's quantity, a
    day order, tagged with its parent."""
    algo = dict(parent.algo or {})
    return replace(
        parent,
        client_id=child_client_id(parent.client_id, piece.seq),
        quantity=float(piece.quantity),
        time_in_force="day",
        algo={"name": algo.get("name"), "parent": parent.client_id},
    )


def start_parent(
    state: SqliteState,
    order: Order,
    *,
    portfolio_id: str,
    ticket_id: str | None,
    now: datetime,
) -> list[ChildSlice]:
    """Record ``order`` (its algo resolved with a window) as a parent with
    its planned slices. Returns the slices. Raises ``ValueError`` when the
    algo has no window or the order is below one share."""
    spec = order.algo or {}
    window = window_of(spec)
    if window is None:
        raise ValueError(f"{order.client_id}: a sliced algo needs a window")
    shares = math.floor(order.quantity + _EPS)
    if shares < 1:
        raise ValueError(f"{order.client_id}: {order.quantity} is below one share")
    algo = get_algo(str(spec["name"]))
    pieces = algo.slices(shares, algo.validate(spec.get("params")), window)
    stamp = now.isoformat(timespec="seconds")
    parent = replace(order, quantity=float(shares))
    with state.transaction():
        state.execute(
            "INSERT INTO algo_parents (client_id, portfolio_id, ticket_id, ticker, side,"
            " quantity, algo, order_json, window_start, window_end, state, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)",
            [
                order.client_id,
                portfolio_id,
                ticket_id,
                order.ticker,
                order.side,
                float(shares),
                algo.name,
                _order_json(parent),
                window.as_dict()["start"],
                window.as_dict()["end"],
                stamp,
                stamp,
            ],
        )
        for piece in pieces:
            state.execute(
                "INSERT INTO algo_slices (parent_client_id, seq, child_client_id, quantity,"
                " send_after) VALUES (?, ?, ?, ?, ?)",
                [
                    order.client_id,
                    piece.seq,
                    child_client_id(order.client_id, piece.seq),
                    float(piece.quantity),
                    piece.send_after.isoformat(timespec="seconds"),
                ],
            )
    _log.info(
        "algo_slices.parent_started", client_id=order.client_id, algo=algo.name, slices=len(pieces)
    )
    return pieces


def _order_json(order: Order) -> str:
    from stonks.production.tickets import order_to_json

    return order_to_json(order)


def parent_order(state: SqliteState, client_id: str) -> Order:
    from stonks.production.tickets import order_from_json

    rows = state.sql("SELECT order_json FROM algo_parents WHERE client_id = ?", [client_id])
    if not rows:
        raise LookupError(f"no parent order {client_id!r}")
    return order_from_json(rows[0]["order_json"])


# ---- the parent's state ------------------------------------------------------------


def parent_state_of(
    total: float,
    filled: float,
    slice_statuses: Sequence[str],
    child_states: Sequence[str | None],
    *,
    cancelled: bool = False,
) -> OrderState:
    """What the children add up to (see the module doc). ``child_states``
    holds the fine state of each sent child. ``cancelled``: a person or the
    kill switch stopped the parent."""
    if filled >= total - _EPS:
        return "filled"
    working = any(s == "planned" for s in slice_statuses) or any(
        s is not None and s not in TERMINAL for s in child_states
    )
    if working:
        return "partially_filled" if filled > _EPS else "accepted"
    if cancelled:
        return "cancelled"
    if filled > _EPS:
        return "expired"
    sent = [s for s in child_states if s is not None]
    if sent and all(s == "rejected" for s in sent):
        return "rejected"
    if any(s == "cancelled" for s in sent):
        return "cancelled"
    return "expired"


@dataclass(frozen=True)
class ParentView:
    """One parent with its slices, for reads and the console."""

    client_id: str
    portfolio_id: str
    ticker: str
    side: str
    quantity: float
    filled: float
    algo: str
    state: str
    window_start: datetime
    window_end: datetime
    slices: tuple[dict[str, Any], ...]


def _children(state: SqliteState, parent_client_id: str) -> list[Any]:
    return state.sql(
        "SELECT s.seq, s.child_client_id, s.quantity, s.send_after, s.status, s.status_reason,"
        " o.state AS child_state, o.status AS child_status,"
        " (SELECT COALESCE(SUM(f.quantity), 0) FROM fills f"
        "  WHERE f.order_client_id = s.child_client_id) AS filled"
        " FROM algo_slices s LEFT JOIN orders o ON o.client_id = s.child_client_id"
        " WHERE s.parent_client_id = ? ORDER BY s.seq",
        [parent_client_id],
    )


def get_parent(state: SqliteState, client_id: str) -> ParentView:
    rows = state.sql("SELECT * FROM algo_parents WHERE client_id = ?", [client_id])
    if not rows:
        raise LookupError(f"no parent order {client_id!r}")
    return _view(state, rows[0])


def list_parents(state: SqliteState, portfolio_ids: Sequence[str]) -> list[ParentView]:
    """Parents of ``portfolio_ids``, newest first."""
    if not portfolio_ids or not parents_recorded(state):
        return []
    marks = ",".join("?" for _ in portfolio_ids)
    rows = state.sql(
        f"SELECT * FROM algo_parents WHERE portfolio_id IN ({marks})"
        " ORDER BY created_at DESC, client_id",
        list(portfolio_ids),
    )
    return [_view(state, r) for r in rows]


def _view(state: SqliteState, row: Any) -> ParentView:
    children = _children(state, row["client_id"])
    return ParentView(
        client_id=row["client_id"],
        portfolio_id=row["portfolio_id"],
        ticker=row["ticker"],
        side=row["side"],
        quantity=float(row["quantity"]),
        filled=sum(float(c["filled"]) for c in children),
        algo=row["algo"],
        state=row["state"],
        window_start=datetime.fromisoformat(row["window_start"]),
        window_end=datetime.fromisoformat(row["window_end"]),
        slices=tuple(
            {
                "seq": int(c["seq"]),
                "client_id": c["child_client_id"],
                "quantity": float(c["quantity"]),
                "send_after": c["send_after"],
                "status": c["status"],
                "status_reason": c["status_reason"],
                "order_state": c["child_state"] or c["child_status"],
                "filled": float(c["filled"]),
            }
            for c in children
        ),
    )


def settle_parent(
    state: SqliteState, client_id: str, now: datetime, *, cancelled: bool = False
) -> OrderState:
    """Move the parent to the state its children add up to. Returns it."""
    row = state.sql("SELECT quantity, state FROM algo_parents WHERE client_id = ?", [client_id])[0]
    current: OrderState = row["state"]
    children = _children(state, client_id)
    filled = sum(float(c["filled"]) for c in children)
    target = parent_state_of(
        float(row["quantity"]),
        filled,
        [c["status"] for c in children],
        [(c["child_state"] or c["child_status"]) if c["status"] == "sent" else None
         for c in children],
        cancelled=cancelled,
    )  # fmt: skip
    if target == current:
        return current
    if not can_transition(current, target):
        _log.warning(
            "algo_slices.illegal_transition", client_id=client_id, current=current, target=target
        )
        return current
    state.execute(
        "UPDATE algo_parents SET state = ?, updated_at = ? WHERE client_id = ? AND state = ?",
        [target, now.isoformat(timespec="seconds"), client_id, current],
    )
    _log.info("algo_slices.parent_state", client_id=client_id, state=target)
    return target


# ---- sending the children ----------------------------------------------------------


@dataclass(frozen=True)
class PortfolioSliceRun:
    portfolio_id: str
    sent: int = 0
    failed: int = 0
    held: int = 0
    skipped: int = 0
    error: str | None = None


@dataclass(frozen=True)
class SliceRun:
    portfolios: tuple[PortfolioSliceRun, ...] = ()

    @property
    def sent(self) -> int:
        return sum(p.sent for p in self.portfolios)

    @property
    def failed(self) -> int:
        return sum(p.failed for p in self.portfolios)

    @property
    def errors(self) -> int:
        return sum(1 for p in self.portfolios if p.error)

    def detail(self) -> dict[str, Any]:
        return {
            "portfolios": len(self.portfolios),
            "sent": self.sent,
            "failed": self.failed,
            "held": sum(p.held for p in self.portfolios),
            "skipped": sum(p.skipped for p in self.portfolios),
            "errors": [f"{p.portfolio_id}: {p.error}" for p in self.portfolios if p.error],
        }


def work_parents(
    state: SqliteState,
    open_broker: Callable[[str], Broker],
    *,
    clock: Clock = SYSTEM_CLOCK,
    portfolio_id: str | None = None,
) -> SliceRun:
    """One pass over every working parent (of ``portfolio_id`` when
    given). Never raises for one portfolio's broker."""
    if not parents_recorded(state):
        return SliceRun()
    marks = ",".join("?" for _ in OPEN_PARENT_STATES)
    sql = f"SELECT client_id, portfolio_id FROM algo_parents WHERE state IN ({marks})"
    params: list[Any] = list(OPEN_PARENT_STATES)
    if portfolio_id is not None:
        sql += " AND portfolio_id = ?"
        params.append(portfolio_id)
    by_portfolio: dict[str, list[str]] = {}
    for row in state.sql(sql + " ORDER BY created_at, client_id", params):
        by_portfolio.setdefault(row["portfolio_id"], []).append(row["client_id"])
    runs = [
        _work_portfolio(state, pid, cids, open_broker, clock) for pid, cids in by_portfolio.items()
    ]
    result = SliceRun(portfolios=tuple(runs))
    _log.info("algo_slices.done", **{k: v for k, v in result.detail().items() if k != "errors"})
    return result


def _work_portfolio(
    state: SqliteState,
    portfolio_id: str,
    parents: list[str],
    open_broker: Callable[[str], Broker],
    clock: Clock,
) -> PortfolioSliceRun:
    log = _log.bind(portfolio_id=portfolio_id)
    try:
        broker = open_broker(portfolio_id)
    except Exception as exc:
        log.warning("algo_slices.broker_unavailable", error=str(exc))
        return PortfolioSliceRun(portfolio_id, error=f"{type(exc).__name__}: {exc}")
    now = clock.now()
    _reconcile_children(state, broker, portfolio_id, parents, now)
    stop_all, stop_buys = _halted(state, portfolio_id, clock)
    sent = failed = held = skipped = 0
    for cid in parents:
        parent = parent_order(state, cid)
        window = window_of(parent.algo or {})
        pieces = state.sql(
            "SELECT seq, quantity, send_after FROM algo_slices"
            " WHERE parent_client_id = ? AND status = 'planned' ORDER BY seq",
            [cid],
        )
        for piece in pieces:
            due = datetime.fromisoformat(piece["send_after"])
            if window is not None and now >= window.end:
                _set_slice(
                    state, cid, int(piece["seq"]), "skipped", now, "not sent by the window end"
                )
                skipped += 1
                continue
            if now < due:
                continue
            if stop_all or (stop_buys and not _reduces(parent)):
                held += 1
                continue
            child = child_order(parent, ChildSlice(int(piece["seq"]), int(piece["quantity"]), due))
            outcome = send_child(state, broker, child, portfolio_id=portfolio_id, clock=clock)
            _set_slice(state, cid, int(piece["seq"]), "sent", now, None if outcome else "refused")
            sent += outcome
            failed += not outcome
        settle_parent(state, cid, clock.now())
    return PortfolioSliceRun(portfolio_id, sent=sent, failed=failed, held=held, skipped=skipped)


def _set_slice(
    state: SqliteState,
    parent: str,
    seq: int,
    status: SliceStatus,
    now: datetime,
    reason: str | None,
) -> None:
    state.execute(
        "UPDATE algo_slices SET status = ?, status_reason = ?,"
        " sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END"
        " WHERE parent_client_id = ? AND seq = ? AND status = 'planned'",
        [status, reason, status, now.isoformat(timespec="seconds"), parent, seq],
    )


def _reduces(order: Order) -> bool:
    if order.position_effect is not None:
        return order.position_effect == "close"
    return order.side == "sell"


def _halted(state: SqliteState, portfolio_id: str, clock: Clock) -> tuple[bool, bool]:
    """(stop every slice, stop the slices that open or grow)."""
    rows = state.sql(
        "SELECT p.owner_id, p.status AS portfolio_status, u.status AS user_status"
        " FROM portfolios p LEFT JOIN users u ON u.id = p.owner_id WHERE p.id = ?",
        [portfolio_id],
    )
    if not rows:
        return True, True
    row = rows[0]
    running = row["portfolio_status"] == "active" and row["user_status"] in (None, "active")
    halts = active_halts(state, today(clock), portfolio_id=portfolio_id, user_id=row["owner_id"])
    return (not running or any(h.halt == "all" for h in halts)), any(
        h.halt == "buys" for h in halts
    )


def _reconcile_children(
    state: SqliteState, broker: Broker, portfolio_id: str, parents: list[str], now: datetime
) -> None:
    if not parents:
        return
    if isinstance(broker, ExecutionSource) or not isinstance(broker, OrderStateSource):
        try:
            reconcile_orders(broker, state, now=now, portfolio_id=portfolio_id)
        except Exception as exc:
            _log.warning("algo_slices.reconcile_failed", error=str(exc))
        return
    marks = ",".join("?" for _ in parents)
    rows = state.sql(
        "SELECT s.child_client_id FROM algo_slices s JOIN orders o"
        " ON o.client_id = s.child_client_id"
        f" WHERE s.parent_client_id IN ({marks}) AND s.status = 'sent'"
        " AND o.status IN ('pending', 'partially_filled')",
        parents,
    )
    for row in rows:
        try:
            reconcile_order(broker, state, row["child_client_id"], now=now, reject_unknown=False)
        except Exception as exc:
            _log.warning(
                "algo_slices.child_reconcile_failed",
                client_id=row["child_client_id"],
                error=str(exc),
            )


def send_child(
    state: SqliteState, broker: Broker, child: Order, *, portfolio_id: str, clock: Clock
) -> bool:
    """Send one child order: committed ``pending``, sent, ``submitted``.
    A refusal marks it ``rejected``, no answer ``unknown`` (reconciliation
    settles it, it is never sent twice). Returns whether it went out."""
    from stonks.production.tick import _record_order

    cid = child.client_id
    if state.sql("SELECT 1 FROM orders WHERE client_id = ?", [cid]):
        return True  # sent before a crash: reconciliation follows it
    with state.transaction():
        _record_order(state, child, status="pending", portfolio_id=portfolio_id)
    try:
        broker.place_order(child)
    except OrderRejectedError as exc:
        _log.warning("algo_slices.child_rejected", client_id=cid, error=str(exc))
        write_state(state, cid, "rejected", reason=str(exc)[:500], clock=clock)
        return False
    except Exception as exc:
        _log.warning("algo_slices.child_no_answer", client_id=cid, error=str(exc))
        mark_unknown(state, cid, f"submit outcome unknown: {exc}"[:500], clock=clock)
        return True
    write_state(state, cid, "submitted", clock=clock)
    if isinstance(broker, OrderStateSource):
        try:
            reconcile_order(broker, state, cid, reject_unknown=False)
        except Exception as exc:
            _log.warning("algo_slices.child_sync_failed", client_id=cid, error=str(exc))
    return True


def cancel_parent(
    state: SqliteState, broker: Broker, client_id: str, *, reason: str, clock: Clock
) -> OrderState:
    """Stop working a parent: planned slices are skipped and open children
    cancelled at the broker. Returns the parent's state after."""
    with state.transaction():
        state.execute(
            "UPDATE algo_slices SET status = 'skipped', status_reason = ?"
            " WHERE parent_client_id = ? AND status = 'planned'",
            [reason[:500], client_id],
        )
        state.execute(
            "UPDATE algo_parents SET state_reason = ? WHERE client_id = ?",
            [reason[:500], client_id],
        )
    if isinstance(broker, OrderCanceller):
        for child in _children(state, client_id):
            if child["status"] == "sent" and (child["child_state"] or "") not in TERMINAL:
                try:
                    broker.cancel_order(child["child_client_id"])
                except Exception as exc:
                    _log.warning(
                        "algo_slices.cancel_failed", client_id=child["child_client_id"],
                        error=str(exc),
                    )  # fmt: skip
        _reconcile_children(
            state, broker, _portfolio_of(state, client_id), [client_id], clock.now()
        )
    return settle_parent(state, client_id, clock.now(), cancelled=True)


def _portfolio_of(state: SqliteState, client_id: str) -> str:
    return state.sql("SELECT portfolio_id FROM algo_parents WHERE client_id = ?", [client_id])[0][
        "portfolio_id"
    ]


__all__ = [
    "ParentView",
    "SliceRun",
    "cancel_parent",
    "child_order",
    "get_parent",
    "is_parent",
    "list_parents",
    "open_parent_count",
    "parent_state_of",
    "parents_recorded",
    "send_child",
    "settle_parent",
    "start_parent",
    "work_parents",
]
