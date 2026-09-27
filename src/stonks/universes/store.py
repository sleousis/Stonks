"""Lake persistence for universe definitions, index histories and the
membership rows a refresh writes (migrations 015 and 016)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from stonks.universes.base import (
    EARLIEST,
    IndexChange,
    IndexHistory,
    MembershipSpan,
    UniverseDefinition,
)

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

_DEF_COLS = (
    "id",
    "kind",
    "name",
    "description",
    "spec_json",
    "created_at",
    "updated_at",
    "refreshed_at",
    "member_count",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _to_date(value: Any) -> date | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return pd.Timestamp(value).date()


def _to_datetime(value: Any) -> datetime | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return pd.Timestamp(value).to_pydatetime()


class UniverseStore:
    """Reads and writes on an open lake. Writers that must be atomic
    together are wrapped in ``lake.transaction()`` by the caller."""

    def __init__(self, lake: DuckDBLake) -> None:
        self._lake = lake

    # ---- definitions -----------------------------------------------------------

    def save(self, definition: UniverseDefinition) -> UniverseDefinition:
        """Create or replace a definition. The spec is validated first;
        ``created_at`` survives a replace, the refresh fields are kept."""
        definition.validated()
        now = _now()
        self._lake.con.execute(
            """
            INSERT INTO universe_definitions
                   (id, kind, name, description, spec_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                   kind = EXCLUDED.kind, name = EXCLUDED.name,
                   description = EXCLUDED.description, spec_json = EXCLUDED.spec_json,
                   updated_at = EXCLUDED.updated_at
            """,
            [
                definition.id,
                definition.kind,
                definition.name,
                definition.description,
                json.dumps(definition.spec, sort_keys=True, default=str),
                now,
                now,
            ],
        )
        return self.get(definition.id)

    def get(self, universe_id: str) -> UniverseDefinition:
        row = self._lake.con.execute(
            f"SELECT {', '.join(_DEF_COLS)} FROM universe_definitions WHERE id = ?",
            [universe_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown universe {universe_id!r}")
        return self._definition(row)

    def exists(self, universe_id: str) -> bool:
        row = self._lake.con.execute(
            "SELECT 1 FROM universe_definitions WHERE id = ?", [universe_id]
        ).fetchone()
        return row is not None

    def list(self) -> list[UniverseDefinition]:
        rows = self._lake.con.execute(
            f"SELECT {', '.join(_DEF_COLS)} FROM universe_definitions ORDER BY id"
        ).fetchall()
        return [self._definition(r) for r in rows]

    def delete(self, universe_id: str) -> None:
        """Drop the definition and its membership rows."""
        self.get(universe_id)
        with self._lake.transaction():
            self._lake.con.execute(
                "DELETE FROM universe_membership WHERE universe_id = ?", [universe_id]
            )
            self._lake.con.execute("DELETE FROM universe_definitions WHERE id = ?", [universe_id])

    def mark_refreshed(self, universe_id: str, member_count: int) -> None:
        self._lake.con.execute(
            "UPDATE universe_definitions SET refreshed_at = ?, member_count = ? WHERE id = ?",
            [_now(), member_count, universe_id],
        )

    @staticmethod
    def _definition(row: tuple) -> UniverseDefinition:
        rec = dict(zip(_DEF_COLS, row, strict=True))
        return UniverseDefinition(
            id=rec["id"],
            kind=rec["kind"],
            name=rec["name"],
            description=rec["description"],
            spec=json.loads(rec["spec_json"]),
            created_at=_to_datetime(rec["created_at"]),
            updated_at=_to_datetime(rec["updated_at"]),
            refreshed_at=_to_datetime(rec["refreshed_at"]),
            member_count=rec["member_count"],
        )

    # ---- membership ------------------------------------------------------------

    def replace_membership(self, universe_id: str, spans: list[MembershipSpan]) -> int:
        """Make ``spans`` the universe's only membership rows. Spans that
        share a start day for one ticker are merged (the longest wins)."""
        merged: dict[tuple[str, date], date | None] = {}
        for s in spans:
            key = (s.ticker, s.start_date)
            if key in merged:
                old = merged[key]
                merged[key] = None if old is None or s.end_date is None else max(old, s.end_date)
            else:
                merged[key] = s.end_date
        with self._lake.transaction():
            self._lake.con.execute(
                "DELETE FROM universe_membership WHERE universe_id = ?", [universe_id]
            )
            if merged:
                self._lake.upsert_universe_membership(
                    pd.DataFrame(
                        [
                            {
                                "universe_id": universe_id,
                                "ticker": t,
                                "start_date": start,
                                "end_date": end,
                            }
                            for (t, start), end in merged.items()
                        ]
                    )
                )
        return len(merged)

    def membership_history(
        self, universe_id: str, *, ticker: str | None = None, limit: int, offset: int = 0
    ) -> tuple[list[MembershipSpan], int]:
        """One page of the universe's spans, latest change first (the end
        of a closed span, else its start), plus the total. ``ticker``
        keeps tickers that contain it, ignoring case."""
        where, params = ["universe_id = ?"], [universe_id]
        if ticker:
            where.append("upper(ticker) LIKE ?")
            params.append(f"%{ticker.upper()}%")
        clause = " AND ".join(where)
        con = self._lake.con
        counted = con.execute(
            f"SELECT count(*) FROM universe_membership WHERE {clause}", params
        ).fetchone()
        total = counted[0] if counted else 0
        rows = con.execute(
            f"""
            SELECT ticker, start_date, end_date FROM universe_membership WHERE {clause}
             ORDER BY COALESCE(end_date, start_date) DESC, ticker, start_date DESC
             LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        spans = [MembershipSpan(r[0], _to_date(r[1]) or EARLIEST, _to_date(r[2])) for r in rows]
        return spans, int(total)

    def exchanges(self) -> list[tuple[str, int, int]]:
        """``(exchange, instruments, listed)`` for every exchange the lake's
        instruments name, by code. ``listed`` leaves out delisted names."""
        rows = self._lake.con.execute(
            """
            SELECT exchange, count(*), count(*) FILTER (WHERE NOT coalesce(is_delisted, false))
              FROM instruments WHERE exchange IS NOT NULL AND exchange <> ''
             GROUP BY exchange ORDER BY exchange
            """
        ).fetchall()
        return [(r[0], int(r[1]), int(r[2])) for r in rows]

    # ---- index histories -------------------------------------------------------

    def save_index_history(self, history: IndexHistory) -> None:
        """Store a snapshot (replacing that day's snapshot) and upsert the
        changes. Changes already stored stay."""
        con = self._lake.con
        with self._lake.transaction():
            if history.as_of is not None:
                con.execute(
                    "DELETE FROM index_constituent_snapshots WHERE index_id = ? AND as_of = ?",
                    [history.index_id, history.as_of],
                )
                for ticker in dict.fromkeys(history.constituents):
                    con.execute(
                        "INSERT INTO index_constituent_snapshots VALUES (?, ?, ?, ?)",
                        [history.index_id, history.as_of, ticker, history.source],
                    )
            for change in dict.fromkeys(history.changes):
                con.execute(
                    """
                    INSERT INTO index_constituent_changes VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT DO UPDATE SET source = EXCLUDED.source
                    """,
                    [
                        history.index_id,
                        change.ticker,
                        change.change_date,
                        change.action,
                        history.source,
                    ],
                )

    def index_history(self, index_id: str) -> IndexHistory | None:
        """The latest snapshot plus every change of ``index_id``; ``None``
        when nothing is stored."""
        con = self._lake.con
        latest = con.execute(
            "SELECT MAX(as_of) FROM index_constituent_snapshots WHERE index_id = ?", [index_id]
        ).fetchone()[0]
        constituents: tuple[str, ...] = ()
        source = None
        if latest is not None:
            rows = con.execute(
                """
                SELECT ticker, source FROM index_constituent_snapshots
                 WHERE index_id = ? AND as_of = ? ORDER BY ticker
                """,
                [index_id, latest],
            ).fetchall()
            constituents = tuple(r[0] for r in rows)
            source = rows[0][1] if rows else None
        changes = con.execute(
            """
            SELECT ticker, change_date, action, source FROM index_constituent_changes
             WHERE index_id = ? ORDER BY change_date DESC, action, ticker
            """,
            [index_id],
        ).fetchall()
        if latest is None and not changes:
            return None
        if source is None and changes:
            source = changes[0][3]
        return IndexHistory(
            index_id=index_id,
            as_of=_to_date(latest),
            constituents=constituents,
            changes=tuple(IndexChange(r[0], _to_date(r[1]), r[2]) for r in changes),
            source=source,
        )

    def index_ids(self) -> list[str]:
        rows = self._lake.con.execute(
            """
            SELECT index_id FROM index_constituent_snapshots
            UNION SELECT index_id FROM index_constituent_changes ORDER BY 1
            """
        ).fetchall()
        return [r[0] for r in rows]
