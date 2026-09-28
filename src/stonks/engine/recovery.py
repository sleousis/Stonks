"""Restart and state recovery for the engine process (roadmap 21.2.5).

The ledger (``orders`` and ``fills``) is the engine's memory. Nothing the
process holds in memory is needed to resume after a crash:

- :class:`EngineRuns` keeps one ``engine_runs`` row per start (migration
  041): status, heartbeat and the checkpoint (the last bar close whose
  orders were routed). A start that finds a row still ``running`` marks it
  ``crashed``, since the process lock says no other engine is up.
- :func:`ledger_portfolio` rebuilds a simulated book from its starting cash
  and the ledger's fills, so a restarted paper book holds what it held.
- :func:`end_lost_orders` closes the open orders of a simulated book: its
  working orders lived in the dead process, so they can never fill. A real
  broker keeps its orders, and the router's startup reconciliation syncs
  them instead.
- :func:`ledger_fills` reads fills in booking order, so the decision step
  learns who owns each holding again.

Client ids are deterministic (book, strategy, bar, ticker, side), and the
router never sends a client id already in the ledger. So a decision made
again after a restart is ``known``, never sent twice.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now
from stonks.core.types import Portfolio
from stonks.execution.brokers.base import QTY_EPSILON
from stonks.execution.order_state import current_state, write_state
from stonks.execution.reconcile import NON_TERMINAL_STATUSES
from stonks.logging import get_logger
from stonks.production.ledger import ledger_filter
from stonks.store.state import SqliteState

_log = get_logger("stonks.engine.recovery")

EngineMode = Literal["live", "replay"]
EngineRunStatus = Literal["running", "stopped", "failed", "crashed"]

#: Why the orders of a dead simulated book were closed.
LOST_REASON = "engine restart: the simulated broker's working orders ended with the process"


@dataclass(frozen=True)
class EngineRun:
    id: str
    session_date: date
    mode: EngineMode
    status: EngineRunStatus
    started_at: str
    last_close_at: datetime | None
    recovered_from: str | None
    bar_closes: int
    orders_routed: int
    pid: int | None
    heartbeat_at: str | None
    finished_at: str | None
    error: str | None

    @classmethod
    def from_row(cls, row: Any) -> EngineRun:
        last = row["last_close_at"]
        return cls(
            id=row["id"],
            session_date=date.fromisoformat(row["session_date"]),
            mode=row["mode"],
            status=row["status"],
            started_at=row["started_at"],
            last_close_at=datetime.fromisoformat(last) if last else None,
            recovered_from=row["recovered_from"],
            bar_closes=int(row["bar_closes"]),
            orders_routed=int(row["orders_routed"]),
            pid=row["pid"],
            heartbeat_at=row["heartbeat_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        )


class EngineRuns:
    """The ``engine_runs`` table. Only the process holding the engine lock
    writes it."""

    def __init__(self, state: SqliteState, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.state = state
        self.clock = clock

    def mark_crashed(self) -> list[EngineRun]:
        """Close every row left ``running`` as ``crashed``. Returns them."""
        rows = [
            EngineRun.from_row(r)
            for r in self.state.sql(
                "SELECT * FROM engine_runs WHERE status = 'running' ORDER BY started_at"
            )
        ]
        for run in rows:
            self.state.execute(
                "UPDATE engine_runs SET status = 'crashed', finished_at = ? WHERE id = ?",
                [run.heartbeat_at or iso_now(self.clock), run.id],
            )
            _log.warning("engine.crashed_run_found", run_id=run.id, last_close=run.last_close_at)
        return rows

    def latest(self, session_date: date | None = None) -> EngineRun | None:
        sql, params = "SELECT * FROM engine_runs", []
        if session_date is not None:
            sql += " WHERE session_date = ?"
            params.append(session_date.isoformat())
        rows = self.state.sql(sql + " ORDER BY started_at DESC, rowid DESC LIMIT 1", params)
        return EngineRun.from_row(rows[0]) if rows else None

    def checkpoint_for(self, session_date: date, mode: EngineMode) -> EngineRun | None:
        """The latest run of the same session and mode that got past a bar
        close: where a restart resumes."""
        rows = self.state.sql(
            "SELECT * FROM engine_runs WHERE session_date = ? AND mode = ?"
            " AND last_close_at IS NOT NULL ORDER BY last_close_at DESC, started_at DESC LIMIT 1",
            [session_date.isoformat(), mode],
        )
        return EngineRun.from_row(rows[0]) if rows else None

    def start(
        self,
        session_date: date,
        mode: EngineMode,
        *,
        pid: int | None = None,
        recovered_from: EngineRun | None = None,
    ) -> str:
        run_id = f"eng_{session_date.isoformat()}_{uuid.uuid4().hex[:8]}"
        now = iso_now(self.clock)
        self.state.execute(
            "INSERT INTO engine_runs (id, session_date, mode, status, pid, started_at,"
            " heartbeat_at, last_close_at, recovered_from)"
            " VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
            [
                run_id,
                session_date.isoformat(),
                mode,
                pid,
                now,
                now,
                recovered_from.last_close_at.isoformat()
                if recovered_from is not None and recovered_from.last_close_at is not None
                else None,
                recovered_from.id if recovered_from is not None else None,
            ],
        )
        return run_id

    def heartbeat(
        self,
        run_id: str,
        *,
        last_close_at: datetime | None = None,
        bar_closes: int | None = None,
        orders_routed: int | None = None,
    ) -> None:
        sets: list[str] = ["heartbeat_at = ?"]
        params: list[Any] = [iso_now(self.clock)]
        if last_close_at is not None:
            sets.append("last_close_at = ?")
            params.append(last_close_at.isoformat())
        if bar_closes is not None:
            sets.append("bar_closes = ?")
            params.append(bar_closes)
        if orders_routed is not None:
            sets.append("orders_routed = ?")
            params.append(orders_routed)
        self.state.execute(
            f"UPDATE engine_runs SET {', '.join(sets)} WHERE id = ?", [*params, run_id]
        )

    def finish(
        self,
        run_id: str,
        status: EngineRunStatus,
        *,
        error: str | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> None:
        self.state.execute(
            "UPDATE engine_runs SET status = ?, finished_at = ?, error = ?, summary_json = ?"
            " WHERE id = ?",
            [
                status,
                iso_now(self.clock),
                error,
                json.dumps(dict(summary), default=str) if summary is not None else None,
                run_id,
            ],
        )


# ---- the ledger --------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerFill:
    id: int
    client_id: str
    ticker: str
    side: str
    quantity: float
    price: float
    fee: float
    strategy_id: str | None


def ledger_fills(state: SqliteState, portfolio_id: str, *, after_id: int = 0) -> list[LedgerFill]:
    """Fills of ``portfolio_id`` with ``fills.id > after_id``, in booking order."""
    where, params = ledger_filter(state, "orders", portfolio_id, alias="o")
    rows = state.sql(
        "SELECT f.id, f.order_client_id, f.ticker, o.side, f.quantity, f.price, f.fee,"
        " o.strategy_id FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE f.id > ? AND {where} ORDER BY f.id",
        [after_id, *params],
    )
    return [
        LedgerFill(
            id=int(r["id"]),
            client_id=r["order_client_id"],
            ticker=r["ticker"],
            side=r["side"],
            quantity=float(r["quantity"]),
            price=float(r["price"]),
            fee=float(r["fee"] or 0.0),
            strategy_id=r["strategy_id"],
        )
        for r in rows
    ]


def ledger_portfolio(state: SqliteState, portfolio_id: str, initial_cash: float) -> Portfolio:
    """A simulated book rebuilt from its starting cash and every fill."""
    cash = float(initial_cash)
    positions: dict[str, float] = {}
    for fill in ledger_fills(state, portfolio_id):
        sign = 1.0 if fill.side == "buy" else -1.0
        cash -= sign * fill.quantity * fill.price + fill.fee
        held = positions.get(fill.ticker, 0.0) + sign * fill.quantity
        if abs(held) <= QTY_EPSILON:
            positions.pop(fill.ticker, None)
        else:
            positions[fill.ticker] = held
    return Portfolio(cash=cash, positions=positions)


def open_orders(state: SqliteState, portfolio_id: str) -> list[str]:
    where, params = ledger_filter(state, "orders", portfolio_id)
    marks = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = state.sql(
        f"SELECT client_id FROM orders WHERE status IN ({marks}) AND {where}"
        " ORDER BY created_at, client_id",
        [*NON_TERMINAL_STATUSES, *params],
    )
    return [r["client_id"] for r in rows]


def end_lost_orders(
    state: SqliteState, portfolio_id: str, *, clock: Clock = SYSTEM_CLOCK
) -> list[str]:
    """End the open orders of a simulated book whose process died: a row
    never sent is ``cancelled``, one that was working is ``expired``.
    Their fills stay booked. Returns the client ids."""
    ended: list[str] = []
    for client_id in open_orders(state, portfolio_id):
        current = current_state(state, client_id)
        if current is None:
            continue
        target = "cancelled" if current in ("pending", "pending_cancel") else "expired"
        write_state(state, client_id, target, reason=LOST_REASON, clock=clock)
        ended.append(client_id)
    if ended:
        _log.info("engine.lost_orders_ended", portfolio_id=portfolio_id, count=len(ended))
    return ended
