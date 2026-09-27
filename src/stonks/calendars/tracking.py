"""The tickers a person follows: what their portfolios hold and what their
watchlists list. The calendar page filters by them and the event alerts
go out for them."""

from __future__ import annotations

import json
from collections.abc import Sequence

from stonks.production.ledger import ledger_filter
from stonks.store.state import SqliteState


def _unique(tickers: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(t for t in tickers if t))


def held_tickers(state: SqliteState, portfolio_ids: Sequence[str]) -> list[str]:
    """Tickers with a non-zero position in the latest snapshot of each
    portfolio, in order of first appearance."""
    out: list[str] = []
    for pid in portfolio_ids:
        where, params = ledger_filter(state, "portfolio_snapshots", pid)
        rows = state.sql(
            f"SELECT positions_json FROM portfolio_snapshots WHERE {where} ORDER BY id DESC LIMIT 1",
            params,
        )
        if not rows:
            continue
        positions = json.loads(rows[0]["positions_json"] or "{}")
        out += [t for t, qty in sorted(positions.items()) if float(qty or 0) != 0]
    return _unique(out)


def owned_portfolio_ids(state: SqliteState, user_id: str) -> list[str]:
    """A person's portfolios that are not archived, oldest first."""
    rows = state.sql(
        "SELECT id FROM portfolios WHERE owner_id = ? AND status != 'archived'"
        " ORDER BY created_at, id",
        [user_id],
    )
    return [r["id"] for r in rows]


def watchlist_tickers(
    state: SqliteState, user_id: str, watchlist_id: str | None = None
) -> list[str]:
    """Tickers on one of the person's watchlists, or on all of them."""
    sql = "SELECT tickers_json FROM watchlists WHERE owner_id = ?"
    params: list[str] = [user_id]
    if watchlist_id is not None:
        sql += " AND id = ?"
        params.append(watchlist_id)
    rows = state.sql(sql + " ORDER BY created_at, id", params)
    out: list[str] = []
    for r in rows:
        out += json.loads(r["tickers_json"] or "[]")
    return _unique(out)


def tracked_tickers(state: SqliteState, user_id: str) -> list[str]:
    """Held first, then watched, each once."""
    held = held_tickers(state, owned_portfolio_ids(state, user_id))
    return _unique(held + watchlist_tickers(state, user_id))


def active_people(state: SqliteState) -> list[str]:
    """Ids of active human users."""
    rows = state.sql("SELECT id FROM users WHERE status = 'active' AND kind = 'human' ORDER BY id")
    return [r["id"] for r in rows]
