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
        target_columns = _columns_by_table(target)
        for table, src_cols in _columns_by_table(source).items():
            if table in skip:
                continue
            if table not in target_columns:
                # Source is ahead of this code's migrations; nothing to
                # write into. Log rather than invent a schema.
                _log.warning("lake_copy.table_missing_in_copy", table=table)
                continue
            shared = set(target_columns[table])
            cols = [c for c in src_cols if c in shared]
            _copy_table(
                source, target, table, cols, filter_on_key=_key(table) in src_cols, tickers=tickers
            )
    except Exception:
        target.close()
        raise
    return target


def _columns_by_table(lake: DuckDBLake) -> dict[str, list[str]]:
    """Column names (in table order) of every base table in ``main``."""
    rows = lake.con.execute(
        "SELECT c.table_name, c.column_name"
        "  FROM information_schema.columns c"
        "  JOIN information_schema.tables t"
        "    ON t.table_schema = c.table_schema AND t.table_name = c.table_name"
        " WHERE c.table_schema = 'main' AND t.table_type = 'BASE TABLE'"
        " ORDER BY c.table_name, c.ordinal_position"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for table, column in rows:
        out.setdefault(table, []).append(column)
    return out


def _key(table: str) -> str:
    return _ID_KEYED_TABLES.get(table, "ticker")


def _copy_table(
    source: DuckDBLake,
    target: DuckDBLake,
    table: str,
    cols: list[str],
    *,
    filter_on_key: bool,
    tickers: list[str],
) -> None:
    col_sql = ", ".join(f'"{c}"' for c in cols)
    if filter_on_key:
        frame = source.con.execute(
            f'SELECT {col_sql} FROM "{table}" WHERE "{_key(table)}" = ANY(?)', [tickers]
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
