"""The trade decision store: why did or didn't we trade (roadmap 23.7).

One ``trade_decisions`` row per tick, book and ticker, from
:func:`stonks.portfolio.explain.explain`. A re-run of the same tick
replaces its rows. :func:`prune_decisions` keeps the table small
(``[production.decisions].keep_days``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from stonks.portfolio.explain import TickerDecision
from stonks.production.decisions_settings import DecisionSettings
from stonks.store.state import SqliteState

__all__ = [
    "DecisionSettings",
    "StoredDecision",
    "count_decisions",
    "load_decisions",
    "prune_decisions",
    "record_decisions",
]


@dataclass(frozen=True)
class StoredDecision:
    tick_id: str
    portfolio_id: str
    as_of: date
    decision: TickerDecision


def _strategies_key(strategies: Sequence[str]) -> str:
    return f",{','.join(strategies)}," if strategies else ""


def record_decisions(
    state: SqliteState,
    *,
    tick_id: str,
    portfolio_id: str,
    as_of: date,
    decisions: Sequence[TickerDecision],
) -> int:
    """Replace the rows of ``(tick_id, portfolio_id)`` with ``decisions``."""
    rows = [
        [
            tick_id,
            portfolio_id,
            as_of.isoformat(),
            d.ticker,
            d.step,
            d.outcome,
            d.strategy_id,
            _strategies_key(d.strategies),
            d.score,
            json.dumps(d.detail, default=str, sort_keys=True),
        ]
        for d in decisions
    ]
    with state.transaction():
        state.execute(
            "DELETE FROM trade_decisions WHERE tick_id = ? AND portfolio_id = ?",
            [tick_id, portfolio_id],
        )
        state.con.executemany(
            "INSERT INTO trade_decisions (tick_id, portfolio_id, as_of, ticker, step, outcome,"
            " strategy_id, strategies, score, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def load_decisions(
    state: SqliteState,
    portfolio_id: str,
    *,
    ticker: str | None = None,
    strategy_id: str | None = None,
    tick_id: str | None = None,
    since: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[StoredDecision]:
    """Rows of one book, newest first (then by ticker)."""
    where, params = _where(portfolio_id, ticker, strategy_id, tick_id, since)
    rows = state.sql(
        "SELECT * FROM trade_decisions WHERE "
        + where
        + " ORDER BY as_of DESC, tick_id DESC, ticker LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [_row(r) for r in rows]


def count_decisions(
    state: SqliteState,
    portfolio_id: str,
    *,
    ticker: str | None = None,
    strategy_id: str | None = None,
    tick_id: str | None = None,
    since: date | None = None,
) -> int:
    where, params = _where(portfolio_id, ticker, strategy_id, tick_id, since)
    return int(state.sql("SELECT COUNT(*) AS n FROM trade_decisions WHERE " + where, params)[0][0])


def prune_decisions(state: SqliteState, *, before: date) -> int:
    """Delete rows older than ``before``; the count deleted."""
    with state.transaction():
        cur = state.execute("DELETE FROM trade_decisions WHERE as_of < ?", [before.isoformat()])
    return int(cur.rowcount or 0)


def _where(
    portfolio_id: str,
    ticker: str | None,
    strategy_id: str | None,
    tick_id: str | None,
    since: date | None,
) -> tuple[str, list[object]]:
    clauses, params = ["portfolio_id = ?"], [portfolio_id]
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker.strip().upper())
    if strategy_id:
        clauses.append("(strategy_id = ? OR instr(strategies, ?) > 0)")
        params += [strategy_id, f",{strategy_id},"]
    if tick_id:
        clauses.append("tick_id = ?")
        params.append(tick_id)
    if since is not None:
        clauses.append("as_of >= ?")
        params.append(since.isoformat())
    return " AND ".join(clauses), list(params)


def _row(r: sqlite3.Row) -> StoredDecision:
    strategies = tuple(s for s in str(r["strategies"] or "").split(",") if s)
    decision = TickerDecision(
        ticker=r["ticker"],
        step=r["step"],
        outcome=r["outcome"],
        strategy_id=r["strategy_id"],
        strategies=strategies,
        score=r["score"],
        detail=json.loads(r["detail_json"] or "{}"),
    )
    return StoredDecision(
        tick_id=r["tick_id"],
        portfolio_id=r["portfolio_id"],
        as_of=date.fromisoformat(r["as_of"]),
        decision=decision,
    )
