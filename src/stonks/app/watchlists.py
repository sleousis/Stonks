"""WatchlistService: a person's own ticker lists (roadmap 13.4).

A watchlist is a named, ordered list of instrument ids. The console uses it
as a quick universe for the lab and as a filter on Today and the charts.

- Every call is scoped to the caller: another person's list reads as
  missing (404), admins included. Lists are never shared.
- Reading needs ``data.read``. Creating, changing and deleting need
  ``portfolio.manage`` (a trader's own workspace, like portfolios).
- Names are unique per person (409 on a clash).
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.accounts.audit import iso_now
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.store.state import SqliteState

#: Most lists a person may keep, and most tickers in one list.
MAX_WATCHLISTS = 50
MAX_TICKERS = 500

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_^=]{0,39}$")


def _clean_tickers(tickers: list[str]) -> list[str]:
    """Trimmed, upper-cased, unique, in the order given."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tickers:
        t = raw.strip().upper()
        if not t:
            continue
        if not _TICKER.match(t):
            raise ValueError(f"{raw!r} is not an instrument id")
        if t not in seen:
            seen.add(t)
            out.append(t)
    if len(out) > MAX_TICKERS:
        raise ValueError(f"a watchlist holds at most {MAX_TICKERS} tickers")
    return out


class WatchlistView(BaseModel):
    id: str
    name: str
    tickers: list[str]
    created_at: datetime
    updated_at: datetime


class WatchlistCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    tickers: list[str] = Field(default_factory=list, max_length=MAX_TICKERS)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("tickers")
    @classmethod
    def _tickers(cls, v: list[str]) -> list[str]:
        return _clean_tickers(v)


class WatchlistUpdate(BaseModel):
    """Rename, replace the tickers, or both. Unset fields stay."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    tickers: list[str] | None = Field(default=None, max_length=MAX_TICKERS)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("tickers")
    @classmethod
    def _tickers(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_tickers(v)


def _view(row: sqlite3.Row) -> WatchlistView:
    r = dict(row)
    return WatchlistView(
        id=r["id"],
        name=r["name"],
        tickers=json.loads(r["tickers_json"] or "[]"),
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


class WatchlistService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def list(self, principal: Principal) -> list[WatchlistView]:
        """Your watchlists, oldest first."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT * FROM watchlists WHERE owner_id = ? ORDER BY created_at, id",
                [principal.user_id],
            )
        return [_view(r) for r in rows]

    def get(self, principal: Principal, watchlist_id: str) -> WatchlistView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            return _view(self._owned(state, principal, watchlist_id))

    def create(self, principal: Principal, body: WatchlistCreate) -> WatchlistView:
        require(principal, Permission.PORTFOLIO_MANAGE)
        now = iso_now()
        wid = f"wl_{secrets.token_hex(6)}"
        with self._ctx.state() as state, state.transaction():
            count = int(
                state.sql(
                    "SELECT COUNT(*) FROM watchlists WHERE owner_id = ?", [principal.user_id]
                )[0][0]
            )
            if count >= MAX_WATCHLISTS:
                raise ValidationError(f"you can keep at most {MAX_WATCHLISTS} watchlists")
            self._check_name(state, principal, body.name, None)
            state.execute(
                "INSERT INTO watchlists (id, owner_id, name, tickers_json, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [wid, principal.user_id, body.name, json.dumps(body.tickers), now, now],
            )
            return _view(self._owned(state, principal, wid))

    def update(
        self, principal: Principal, watchlist_id: str, body: WatchlistUpdate
    ) -> WatchlistView:
        require(principal, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state, state.transaction():
            row = dict(self._owned(state, principal, watchlist_id))
            name = body.name if body.name is not None else row["name"]
            tickers = body.tickers if body.tickers is not None else json.loads(row["tickers_json"])
            if name != row["name"]:
                self._check_name(state, principal, name, watchlist_id)
            state.execute(
                "UPDATE watchlists SET name = ?, tickers_json = ?, updated_at = ? WHERE id = ?",
                [name, json.dumps(tickers), iso_now(), watchlist_id],
            )
            return _view(self._owned(state, principal, watchlist_id))

    def delete(self, principal: Principal, watchlist_id: str) -> None:
        require(principal, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state, state.transaction():
            self._owned(state, principal, watchlist_id)
            state.execute("DELETE FROM watchlists WHERE id = ?", [watchlist_id])

    @staticmethod
    def _owned(state: SqliteState, principal: Principal, watchlist_id: str) -> sqlite3.Row:
        rows = state.sql(
            "SELECT * FROM watchlists WHERE id = ? AND owner_id = ?",
            [watchlist_id, principal.user_id],
        )
        if not rows:
            raise NotFoundError(f"no watchlist with id {watchlist_id!r}")
        return rows[0]

    @staticmethod
    def _check_name(
        state: SqliteState, principal: Principal, name: str, except_id: str | None
    ) -> None:
        rows = state.sql(
            "SELECT id FROM watchlists WHERE owner_id = ? AND name = ? AND id != ?",
            [principal.user_id, name, except_id or ""],
        )
        if rows:
            raise ConflictError(f"you already have a watchlist named {name!r}")
