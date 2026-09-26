"""In-memory copies of a lake scoped to a universe, for survival tests that
backtest against modified bars (permuted, perturbed).

The copy holds every table a strategy may read — instruments, the three
statement tables, dividends, analyst data, per-class profiles, macro
series, … — so a strategy sees the same non-price data in the modified
runs as in the real one. Tables are discovered from the source's catalog,
so tables added by future migrations are picked up without edits here.

Filtering: tables with a ``ticker`` column keep only universe rows;
``instruments`` is filtered on ``id``; tables with neither (macro
series) are copied whole. ``bars`` is left empty for the caller to fill
with its modified prices. Operational tables (``ingest_runs``,
``schema_migrations``) are never copied.

Transport goes through the source connection's DataFrame fetch rather
than ``ATTACH``: DuckDB refuses to attach a database file that another
connection in the same process already holds open, and the source may
itself be in-memory.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lab.lake_copy")

#: Never copied: pure bookkeeping, no strategy reads them.
_OPERATIONAL_TABLES = frozenset({"ingest_runs", "schema_migrations"})

#: Tables keyed on the instrument id under a name other than ``ticker``.
_ID_KEYED_TABLES = {"instruments": "id"}


def copy_universe_lake(
    source: DuckDBLake,
    universe: Sequence[str],
    *,
    skip_tables: Iterable[str] = ("bars",),
) -> DuckDBLake:
    """Return a migrated in-memory ``DuckDBLake`` holding ``source``'s
    tables filtered to ``universe`` (``skip_tables`` left empty).

    The caller owns the returned lake; it supports ``with`` and must be
    closed. ``source`` is only read.
    """
    tickers = list(universe)
    skip = set(skip_tables) | _OPERATIONAL_TABLES
    target = DuckDBLake(Path(":memory:"))
    try:
        target.migrate()
        target_tables = set(_base_tables(target))
        for table in _base_tables(source):
            if table in skip:
                continue
            if table not in target_tables:
                # Source is ahead of this code's migrations; nothing to
                # write into. Log rather than invent a schema.
                _log.warning("lake_copy.table_missing_in_copy", table=table)
                continue
            _copy_table(source, target, table, tickers)
    except Exception:
        target.close()
        raise
    return target


def _base_tables(lake: DuckDBLake) -> list[str]:
    rows = lake.con.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def _columns(lake: DuckDBLake, table: str) -> list[str]:
    rows = lake.con.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def _copy_table(source: DuckDBLake, target: DuckDBLake, table: str, tickers: list[str]) -> None:
    src_cols = _columns(source, table)
    cols = [c for c in src_cols if c in set(_columns(target, table))]
    col_sql = ", ".join(f'"{c}"' for c in cols)
    key = _ID_KEYED_TABLES.get(table, "ticker")
    if key in src_cols:
        frame = source.con.execute(
            f'SELECT {col_sql} FROM "{table}" WHERE "{key}" = ANY(?)', [tickers]
        ).fetchdf()
    else:
        frame = source.con.execute(f'SELECT {col_sql} FROM "{table}"').fetchdf()
    if frame.empty:
        return
    target.con.register("_lake_copy_src", frame)
    try:
        target.con.execute(
            f'INSERT INTO "{table}" ({col_sql}) SELECT {col_sql} FROM _lake_copy_src'
        )
    finally:
        target.con.unregister("_lake_copy_src")
