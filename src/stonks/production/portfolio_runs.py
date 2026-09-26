"""``portfolio_runs``: what one tick did to one portfolio (roadmap 15.5, S6).

The tick writes one row per portfolio it trades (a broker portfolio's paper
account is a portfolio of its own): the mode, the status, counts, whether a
risk halt was in force and which paper and auto subscriptions took part.
The auto gate counts a subscription's paper days from these rows
(:func:`stonks.accounts.paper.paper_days_completed`). A same-day re-run of
the same tick id replaces its row; a new tick id adds one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from stonks.store.state import SqliteState

RunMode = Literal["legacy", "paper", "auto"]
RunStatus = Literal["ok", "partial", "error", "noop", "paused"]

TABLE = "portfolio_runs"


@dataclass(frozen=True)
class PortfolioRun:
    tick_id: str
    portfolio_id: str
    as_of: date
    mode: RunMode
    status: RunStatus
    started_at: str
    finished_at: str
    orders_placed: int = 0
    fills: int = 0
    orders_rejected: int = 0
    #: A risk halt (circuit breaker, drawdown) was in force for the book.
    risk_breached: bool = False
    halted: Literal["buys", "all"] | None = None
    error: str | None = None
    paper_subscriptions: Sequence[str] = ()
    auto_subscriptions: Sequence[str] = ()

    @classmethod
    def from_row(cls, row: Any) -> PortfolioRun:
        return cls(
            tick_id=row["tick_id"],
            portfolio_id=row["portfolio_id"],
            as_of=date.fromisoformat(row["as_of"]),
            mode=row["mode"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            orders_placed=int(row["orders_placed"]),
            fills=int(row["fills"]),
            orders_rejected=int(row["orders_rejected"]),
            risk_breached=bool(row["risk_breached"]),
            halted=row["halted"],
            error=row["error"],
            paper_subscriptions=tuple(json.loads(row["paper_subscriptions_json"])),
            auto_subscriptions=tuple(json.loads(row["auto_subscriptions_json"])),
        )


def runs_recorded(state: SqliteState) -> bool:
    """The table exists (migration 020)."""
    return bool(state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE]))


def record_run(state: SqliteState, run: PortfolioRun) -> None:
    state.execute(
        f"""
        INSERT INTO {TABLE}
            (tick_id, portfolio_id, as_of, mode, status, orders_placed, fills,
             orders_rejected, risk_breached, halted, error, paper_subscriptions_json,
             auto_subscriptions_json, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tick_id, portfolio_id) DO UPDATE SET
            mode = excluded.mode, status = excluded.status,
            orders_placed = excluded.orders_placed, fills = excluded.fills,
            orders_rejected = excluded.orders_rejected,
            risk_breached = excluded.risk_breached, halted = excluded.halted,
            error = excluded.error,
            paper_subscriptions_json = excluded.paper_subscriptions_json,
            auto_subscriptions_json = excluded.auto_subscriptions_json,
            finished_at = excluded.finished_at
        """,
        [
            run.tick_id,
            run.portfolio_id,
            run.as_of.isoformat(),
            run.mode,
            run.status,
            run.orders_placed,
            run.fills,
            run.orders_rejected,
            int(run.risk_breached),
            run.halted,
            run.error,
            json.dumps(sorted(run.paper_subscriptions)),
            json.dumps(sorted(run.auto_subscriptions)),
            run.started_at,
            run.finished_at,
        ],
    )


def list_runs(state: SqliteState, portfolio_id: str, *, limit: int = 100) -> list[PortfolioRun]:
    """The portfolio's runs, newest first."""
    rows = state.sql(
        f"SELECT * FROM {TABLE} WHERE portfolio_id = ? ORDER BY as_of DESC, id DESC LIMIT ?",
        [portfolio_id, limit],
    )
    return [PortfolioRun.from_row(r) for r in rows]
