"""DuckDB-backed analytical data lake: prices, financial statements,
extended metadata, ingest run ledger.

Schema lives in ``migrations_duckdb/*.sql``; versions are tracked in
``schema_migrations`` and applied in lexical order at ``migrate()`` time.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.timeutil import day_end, day_start
from stonks.logging import get_logger
from stonks.store.bars import (
    BAR_COLUMNS,
    BAR_KEY,
    BarBackend,
    BarStore,
    DuckDBTableBarStore,
    ParquetBarStore,
    checksums,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations_duckdb"

# Env var that lets an operator opt into a destructive migration
# (``DROP TABLE`` / ``ALTER TABLE ... DROP COLUMN``) against data that
# currently exists. The default is to refuse — losing committed data must
# be an explicit choice, not something a routine ``stonks db init`` does
# silently.
_DESTRUCTIVE_OPT_IN_ENV = "STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS"
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_DROP_TABLE_RE = re.compile(
    rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?({_IDENT})\b",
    re.IGNORECASE,
)
_DROP_COLUMN_RE = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?({_IDENT})\s+"
    rf"DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?({_IDENT})\b",
    re.IGNORECASE,
)
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# (migration file stem, target) pairs whose drop is known to be
# data-preserving because the same migration copies the data elsewhere
# first. ``target`` is a table name for DROP TABLE, ``table.column`` for
# DROP COLUMN. Shipped migrations can't be edited, and the guard only sees
# the pre-migration state, so it can't infer this from the SQL; it trusts
# this list instead. Keyed by the full file stem (not just the version) so
# a different file that happens to reuse a number isn't waved through.
#
# Shipped column drops that are deliberately NOT listed: 005 drops
# tickers.{beta, short_percent, insider_ownership_percent,
# institutional_ownership_percent, employee_count, esg_score} without
# copying the values, so a lake holding them must opt in. A fresh
# ``db init`` is unaffected because those tables are empty at that point.
_DATA_PRESERVING_DROPS: frozenset[tuple[str, str]] = frozenset(
    {
        # 003 INSERTs every prices row into bars (interval='1d') and then
        # recreates prices as a view over bars.
        ("003_intraday_bars", "prices"),
        # Rebuilds insider_transactions with a NULL-safe natural key; rows
        # are staged in a temp table and copied back (only exact natural-key
        # duplicates collapse, last-inserted wins).
        ("010_insider_natural_key", "insider_transactions"),
    }
)

_PRICE_COLS = ("ticker", "date", "open", "high", "low", "close", "adj_close", "volume")
_BAR_COLS = BAR_COLUMNS
_BAR_PK: tuple[str, ...] = BAR_KEY
# The ``bars`` table as migration 003 creates it; used to rebuild it when a
# lake moves its bars back from Parquet. MUST match 003_intraday_bars.sql.
_BARS_TABLE_DDL = """
CREATE TABLE {name} (
    ticker    VARCHAR   NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    interval  VARCHAR   NOT NULL,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    adj_close DOUBLE,
    volume    BIGINT,
    PRIMARY KEY (ticker, timestamp, interval)
)
"""
_BAR_BACKEND_KEY = "bars_backend"
_MIGRATE_HINT = (
    "run `python -m stonks.store.bars_migrate` (or DuckDBLake.migrate_bars_to_parquet / "
    "migrate_bars_to_duckdb) to move the bars first"
)


@dataclass(frozen=True)
class BarsMigrationReport:
    """What a bar-store migration moved: ``series`` (ticker, interval)
    pairs, ``rows`` bars, ``files`` Parquet partition files involved."""

    backend: BarBackend
    series: int
    rows: int
    files: int
    seconds: float


# Wide column lists for each financial-statement table (migration 008).
# Source of truth is the SQL migration; if the two diverge an upsert will
# raise on the missing/extra column at INSERT time, which is loud enough.
_INCOME_STATEMENT_COLS: tuple[str, ...] = (
    "ticker",
    "period_end",
    "frequency",
    "filing_date",
    "currency",
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "research_development",
    "selling_general_administrative",
    "selling_marketing_expenses",
    "other_operating_expenses",
    "total_operating_expenses",
    "operating_income",
    "interest_income",
    "interest_expense",
    "net_interest_income",
    "non_operating_income_other",
    "total_other_income_expense_net",
    "income_before_tax",
    "income_tax_expense",
    "tax_provision",
    "minority_interest",
    "net_income_continuing",
    "discontinued_operations",
    "extraordinary_items",
    "non_recurring",
    "other_items",
    "effect_of_accounting_charges",
    "net_income",
    "net_income_to_common",
    "preferred_stock_adjustments",
    "ebit",
    "ebitda",
    "depreciation_amortization",
    "reconciled_depreciation",
)
_BALANCE_SHEET_COLS: tuple[str, ...] = (
    "ticker",
    "period_end",
    "frequency",
    "filing_date",
    "currency",
    "total_assets",
    "current_assets",
    "cash",
    "cash_and_equivalents",
    "cash_and_short_term_investments",
    "short_term_investments",
    "net_receivables",
    "inventory",
    "other_current_assets",
    "non_current_assets",
    "long_term_investments",
    "property_plant_equipment_net",
    "property_plant_equipment_gross",
    "accumulated_depreciation",
    "accumulated_amortization",
    "goodwill",
    "intangible_assets",
    "other_assets",
    "deferred_long_term_asset_charges",
    "non_current_assets_other",
    "earning_assets",
    "total_liabilities",
    "current_liabilities",
    "accounts_payable",
    "current_deferred_revenue",
    "short_term_debt",
    "short_long_term_debt",
    "short_long_term_debt_total",
    "other_current_liabilities",
    "non_current_liabilities",
    "long_term_debt",
    "long_term_debt_total",
    "capital_lease_obligations",
    "deferred_long_term_liabilities",
    "other_liabilities",
    "non_current_liabilities_other",
    "negative_goodwill",
    "warrants",
    "preferred_stock_redeemable",
    "total_stockholder_equity",
    "common_stock",
    "capital_stock",
    "additional_paid_in_capital",
    "retained_earnings",
    "treasury_stock",
    "accumulated_other_comprehensive_income",
    "other_stockholder_equity",
    "common_stock_total_equity",
    "preferred_stock_total_equity",
    "retained_earnings_total_equity",
    "capital_surplus",
    "total_permanent_equity",
    "noncontrolling_interest",
    "temporary_equity_redeemable_noncontrolling",
    "liabilities_and_stockholders_equity",
    "net_debt",
    "net_tangible_assets",
    "net_working_capital",
    "investments",
    "common_stock_shares_outstanding",
)
_CASH_FLOW_STATEMENT_COLS: tuple[str, ...] = (
    "ticker",
    "period_end",
    "frequency",
    "filing_date",
    "currency",
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "net_income",
    "depreciation",
    "stock_based_compensation",
    "change_in_working_capital",
    "change_to_inventory",
    "change_to_account_receivables",
    "change_to_liabilities",
    "change_to_operating_activities",
    "change_to_net_income",
    "change_receivables",
    "cash_flows_other_operating",
    "other_non_cash_items",
    "capital_expenditures",
    "investments",
    "other_cash_flows_investing",
    "dividends_paid",
    "net_borrowings",
    "issuance_of_capital_stock",
    "sale_purchase_of_stock",
    "other_cash_flows_financing",
    "change_in_cash",
    "cash_and_cash_equivalents_changes",
    "begin_period_cash_flow",
    "end_period_cash_flow",
    "exchange_rate_changes",
    "free_cash_flow",
)
_STATEMENT_PK: tuple[str, ...] = ("ticker", "period_end", "frequency")

# NULL-safe natural key for insider_transactions. Each part renders as
# 'v' || value (or 'n' for NULL) so NULL and '' differ, and DATE / DOUBLE
# normalization makes 100 and 100.0 hash alike. MUST stay identical to the
# expression in migrations_duckdb/010_insider_natural_key.sql, which
# back-filled the key for rows that predate it.
_INSIDER_NATURAL_KEY_SQL = """md5(concat_ws(chr(31),
    COALESCE('v' || ticker, 'n'),
    COALESCE('v' || CAST(CAST(transaction_date AS DATE) AS VARCHAR), 'n'),
    COALESCE('v' || owner_name, 'n'),
    COALESCE('v' || transaction_code, 'n'),
    COALESCE('v' || CAST(CAST(shares AS DOUBLE) AS VARCHAR), 'n'),
    COALESCE('v' || sec_link, 'n')
))"""


def _last_per_key(df: pd.DataFrame, keys: tuple[str, ...]) -> pd.DataFrame:
    """Drop rows whose ``keys`` repeat within the batch, keeping the last.

    ``INSERT ... ON CONFLICT DO UPDATE`` can't apply two input rows to the
    same target row: depending on the DuckDB version it either raises
    ("can not update the same row twice") or silently keeps the first.
    Vendors do send such batches (EODHD's annual and quarterly
    shares-outstanding lists both carry the fiscal-year-end date), so we
    collapse them up front with last-write-wins semantics. pandas treats
    NaN/None as equal here, which matches our "NULLs equal" natural keys.
    """
    return df.drop_duplicates(subset=list(keys), keep="last")


def _changed_snapshots(
    batch: pd.DataFrame,
    latest: pd.DataFrame,
    identity_cols: list[str],
    value_cols: list[str],
    snapshot_col: str,
) -> pd.DataFrame:
    """Rows of ``batch`` that :meth:`DuckDBLake._upsert_on_change` writes.

    A row is kept when its identity has no stored row, when it is not newer
    than the stored latest (a correction or a backfill), or when one of
    ``value_cols`` changed against the last row kept before it (the stored
    latest, then earlier batch rows in date order).

    NULL-drift suppression (I10): a vendor briefly returning NULL for a
    known value is not a change worth recording, it would fill the series
    with vendor flakiness. A column counts as changed only when the new
    value is non-NULL and differs from the prior. NULL to a real value is a
    change (we just learned it), the other way is not.
    """

    def changed(prev: Any, new: Any) -> bool:
        if pd.isna(new):
            return False
        return bool(pd.isna(prev) or prev != new)

    stored = {tuple(r[c] for c in identity_cols): r for r in latest.to_dict("records")}
    batch = batch.reset_index(drop=True)
    keep: list[Any] = []
    ordered = batch.sort_values(snapshot_col, kind="stable")
    for ident, group in ordered.groupby(identity_cols, sort=False, dropna=False):
        ident = ident if isinstance(ident, tuple) else (ident,)
        prev = stored.get(ident)
        last_date = None if prev is None else pd.Timestamp(prev[snapshot_col])
        for idx, row in group.iterrows():
            if prev is None or pd.Timestamp(row[snapshot_col]) <= last_date:
                keep.append(idx)
                if prev is None:
                    prev, last_date = row, pd.Timestamp(row[snapshot_col])
                continue
            if any(changed(prev[c], row[c]) for c in value_cols):
                keep.append(idx)
                prev, last_date = row, pd.Timestamp(row[snapshot_col])
    return batch.loc[sorted(keep)]


_STATEMENT_TABLES = frozenset({"income_statement", "balance_sheet", "cash_flow_statement"})


def _statement_table(statement: str) -> str:
    if statement not in _STATEMENT_TABLES:
        raise ValueError(
            f"unknown statement {statement!r}; expected one of {sorted(_STATEMENT_TABLES)}"
        )
    return statement


def _non_negative_lag(days: int) -> int:
    days = int(days)
    if days < 0:
        raise ValueError(f"lag must be >= 0 days, got {days}")
    return days


def _as_calendar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    raise TypeError(f"expected a date or datetime, got {type(value).__name__}")


def _verify_checksums(
    expected: dict[tuple[str, str], tuple[int, int]],
    got: dict[tuple[str, str], tuple[int, int]],
) -> None:
    """Raise when two bar stores disagree on any (ticker, interval)."""
    if expected == got:
        return
    bad = sorted(k for k in expected.keys() | got.keys() if expected.get(k) != got.get(k))
    shown = ", ".join(f"{t}@{i}" for t, i in bad[:5])
    raise RuntimeError(
        f"bar-store migration checksum mismatch on {len(bad)} series ({shown}"
        f"{', ...' if len(bad) > 5 else ''}); nothing was switched"
    )


def _dates_to_python(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
    """DuckDB hands DATE columns to pandas as timestamps; callers compare
    them with ``datetime.date``, so convert (NULL stays ``None``)."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.Series(
                [None if pd.isna(v) else pd.Timestamp(v).date() for v in df[c]],
                index=df.index,
                dtype=object,
            )
    return df


class DuckDBLake:
    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        bar_backend: BarBackend | None = None,
    ):
        """Open (read-write by default) the lake at ``path``.

        ``read_only=True`` opens an existing file without taking DuckDB's
        exclusive write lock, so any number of processes can read it at
        once (lab worker processes on a snapshot); every write raises. A
        missing file then raises instead of being created.

        Bars live in a :class:`~stonks.store.bars.BarStore`: the ``bars``
        table (``"duckdb"``) or Parquet partitions under ``<lake dir>/bars``
        (``"parquet"``). The lake records which one it uses
        (``lake_settings``), so callers normally pass no ``bar_backend``.
        Passing one asserts it: a fresh or bar-less lake adopts it, while a
        lake whose bars sit in the other store raises and points at
        ``python -m stonks.store.bars_migrate``. Either way the ``bars``
        relation (a view over the files for Parquet) and the ``prices``
        view keep working in SQL."""
        self._path = Path(path)
        self.read_only = read_only
        if not read_only:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con: duckdb.DuckDBPyConnection | None = duckdb.connect(
            str(self._path), read_only=read_only
        )
        # Pin the session to UTC so tz-aware inputs (e.g. UTC intraday
        # timestamps from EODHD) aren't silently shifted into the host's
        # local time when they land in naive-TIMESTAMP columns.
        self._con.execute("SET TimeZone = 'UTC'")
        self._column_type_cache: dict[str, dict[str, str]] = {}
        self._requested_bar_backend = bar_backend
        try:
            self._bars: BarStore = self._open_bar_store()
            self._reconcile_bar_backend()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> DuckDBLake:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise RuntimeError("DuckDBLake connection is closed")
        return self._con

    def export_database(self, target: str | Path, *, bar_backend: BarBackend | None = None) -> None:
        """Write every table and view of this lake to a new standalone
        DuckDB file at ``target`` (which must not exist), then release it.

        Used to materialise a lab snapshot (typically from an in-memory,
        universe-scoped copy) that worker processes open read-only.

        The copy's bars go to ``bar_backend`` (default: this lake's). For
        ``"parquet"`` they land under ``<target dir>/bars``: partitions of a
        Parquet lake are hard-linked or copied, table bars are written out."""
        target = Path(target)
        if target.exists():
            raise FileExistsError(target)
        backend = bar_backend or self.bar_backend
        target_root = target.parent / "bars"
        if backend == "parquet" and target_root.exists():
            raise FileExistsError(target_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self.con.execute("SELECT current_database()").fetchone()[0]
        alias = "_stonks_export"
        path_sql = str(target).replace("'", "''")
        self.con.execute(f"ATTACH '{path_sql}' AS {alias}")
        try:
            self.con.execute(f'COPY FROM DATABASE "{source}" TO {alias}')
            if backend == "parquet":
                self.con.execute(f"DROP TABLE IF EXISTS {alias}.bars")
                self.con.execute(
                    f"INSERT OR REPLACE INTO {alias}.lake_settings VALUES (?, 'parquet')",
                    [_BAR_BACKEND_KEY],
                )
            elif self.bar_backend == "parquet":
                self.con.execute(_BARS_TABLE_DDL.format(name=f"{alias}.bars"))
                self.con.execute(f"INSERT INTO {alias}.bars SELECT * FROM bars")
                self.con.execute(
                    f"DELETE FROM {alias}.lake_settings WHERE key = ?", [_BAR_BACKEND_KEY]
                )
        finally:
            self.con.execute(f"DETACH {alias}")
        if backend == "parquet":
            if isinstance(self._bars, ParquetBarStore):
                self._bars.export_partitions(target_root)
            else:
                copy = ParquetBarStore(target_root, self.con)
                copy.ensure_layout()
                self._copy_table_bars(copy)

    # ---- bar store ------------------------------------------------------------

    @property
    def bar_backend(self) -> BarBackend:
        """Which store holds this lake's bars (``"duckdb"`` or ``"parquet"``)."""
        return self._bars.backend

    @property
    def bar_store(self) -> BarStore:
        return self._bars

    @property
    def bars_root(self) -> Path:
        """Where the Parquet bar store keeps (or would keep) its files."""
        return self._path.parent / "bars"

    def migrate_bars_to_parquet(self) -> BarsMigrationReport:
        """Copy every bar from the ``bars`` table into Parquet partitions
        under :attr:`bars_root`, verify row counts and checksums per
        (ticker, interval), then switch this lake to the Parquet store and
        drop the table. On a mismatch nothing is switched and the table is
        kept; files already written stay (a re-run overwrites them)."""
        if self.bar_backend == "parquet":
            raise RuntimeError("the lake's bars are already in Parquet")
        self._require_parquet_capable()
        if "bars" not in self._base_tables():
            raise RuntimeError("the lake is not migrated yet; call migrate() first")
        started = time.perf_counter()
        store = ParquetBarStore(self.bars_root, self.con)
        store.ensure_layout()
        self._copy_table_bars(store)
        expected = checksums(self.con, "SELECT * FROM main.bars")
        _verify_checksums(expected, store.series_checksums())
        self._switch_to_parquet(store)
        return BarsMigrationReport(
            backend="parquet",
            series=len(expected),
            rows=sum(n for n, _ in expected.values()),
            files=len(store.files()),
            seconds=round(time.perf_counter() - started, 3),
        )

    def migrate_bars_to_duckdb(self) -> BarsMigrationReport:
        """The reverse of :meth:`migrate_bars_to_parquet`: rebuild the
        ``bars`` table from the Parquet files, verify, and switch back. The
        Parquet files are left in place (delete :attr:`bars_root` by hand
        once satisfied)."""
        if not isinstance(self._bars, ParquetBarStore):
            raise RuntimeError("the lake's bars are already in the DuckDB table")
        if self.read_only:
            raise duckdb.InvalidInputException("a read-only lake cannot switch bar stores")
        started = time.perf_counter()
        store = self._bars
        expected = store.series_checksums()
        # The session's ``bars`` view would shadow the new table.
        self.con.execute("DROP VIEW IF EXISTS temp.main.bars")
        try:
            with self.transaction():
                self.con.execute(_BARS_TABLE_DDL.format(name="main.bars"))
                self.con.execute(
                    f"INSERT INTO main.bars SELECT {', '.join(BAR_COLUMNS)} "
                    f"FROM ({store.view_sql()})"
                )
                _verify_checksums(expected, checksums(self.con, "SELECT * FROM main.bars"))
                self.con.execute("DELETE FROM lake_settings WHERE key = ?", [_BAR_BACKEND_KEY])
        except BaseException:
            self._install_bar_view(store)
            raise
        self._column_type_cache.pop("bars", None)
        self._bars = DuckDBTableBarStore(self)
        return BarsMigrationReport(
            backend="duckdb",
            series=len(expected),
            rows=sum(n for n, _ in expected.values()),
            files=len(store.files()),
            seconds=round(time.perf_counter() - started, 3),
        )

    def _open_bar_store(self) -> BarStore:
        if self._persisted_bar_backend() == "parquet":
            store = ParquetBarStore(self.bars_root, self.con, read_only=self.read_only)
            self._install_bar_view(store)
            return store
        return DuckDBTableBarStore(self)

    def _persisted_bar_backend(self) -> BarBackend:
        if "lake_settings" not in self._base_tables():
            return "duckdb"
        row = self.con.execute(
            "SELECT value FROM lake_settings WHERE key = ?", [_BAR_BACKEND_KEY]
        ).fetchone()
        value = row[0] if row else "duckdb"
        if value not in ("duckdb", "parquet"):
            raise ValueError(f"lake_settings.{_BAR_BACKEND_KEY} holds unknown value {value!r}")
        return value

    def _reconcile_bar_backend(self) -> None:
        """Apply the ``bar_backend`` the caller asked for, if any (see
        ``__init__``). Deferred until ``migrate()`` on an unmigrated lake."""
        wanted = self._requested_bar_backend
        if wanted is None or wanted == self.bar_backend:
            return
        if wanted == "duckdb":
            raise RuntimeError(
                "this lake keeps its bars in Parquet; call migrate_bars_to_duckdb() "
                "or run `python -m stonks.store.bars_migrate --to duckdb` first"
            )
        self._require_parquet_capable()
        tables = self._base_tables()
        if "bars" not in tables or "lake_settings" not in tables:
            return  # not migrated yet: migrate() calls back
        held = int(self.con.execute("SELECT COUNT(*) FROM main.bars").fetchone()[0])
        if held:
            raise RuntimeError(
                f"this lake holds {held} bars in its DuckDB table but Parquet was requested; "
                + _MIGRATE_HINT
            )
        store = ParquetBarStore(self.bars_root, self.con)
        store.ensure_layout()
        self._switch_to_parquet(store)

    def _require_parquet_capable(self) -> None:
        if str(self._path) == ":memory:":
            raise ValueError("an in-memory lake cannot keep its bars in Parquet")
        if self.read_only:
            raise duckdb.InvalidInputException("a read-only lake cannot switch bar stores")

    def _switch_to_parquet(self, store: ParquetBarStore) -> None:
        with self.transaction():
            self.con.execute("DROP TABLE main.bars")
            self.con.execute(
                "INSERT OR REPLACE INTO lake_settings VALUES (?, 'parquet')", [_BAR_BACKEND_KEY]
            )
        self._column_type_cache.pop("bars", None)
        self._bars = store
        self._install_bar_view(store)

    def _install_bar_view(self, store: ParquetBarStore) -> None:
        """Expose the Parquet bars as a session-local ``bars`` view, so SQL
        written against the table (and the ``prices`` view) runs unchanged."""
        self.con.execute(f"CREATE OR REPLACE TEMP VIEW bars AS {store.view_sql()}")

    def _copy_table_bars(self, store: ParquetBarStore) -> None:
        """Write every row of the ``bars`` table into ``store``, one
        (interval, ticker) series at a time."""
        series = self.con.execute(
            "SELECT DISTINCT interval, ticker FROM main.bars ORDER BY ALL"
        ).fetchall()
        for interval, ticker in series:
            t = ticker.replace("'", "''")
            i = interval.replace("'", "''")
            store.upsert_query(f"SELECT * FROM main.bars WHERE ticker = '{t}' AND interval = '{i}'")

    def _base_tables(self) -> set[str]:
        rows = self.con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = 'main'"
        ).fetchall()
        return {r[0] for r in rows}

    # ---- schema / migrations ------------------------------------------------

    def migrate(self) -> None:
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
        )
        applied = {
            row[0] for row in self.con.execute("SELECT version FROM schema_migrations").fetchall()
        }
        log = get_logger("stonks.store.lake.migrate")
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.stem.split("_", 1)[0])
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            self._guard_destructive_drops(sql, version=version, name=path.stem, log=log)
            self.con.execute("BEGIN")
            try:
                self.con.execute(sql)
                self.con.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    [version, datetime.now(UTC)],
                )
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
            finally:
                # The migration may have reshaped any table.
                self._column_type_cache.clear()
        self._reconcile_bar_backend()

    def _guard_destructive_drops(self, sql: str, *, version: int, name: str, log: Any) -> None:
        """Refuse to apply a migration whose ``DROP TABLE`` (incl.
        ``CASCADE``) or ``ALTER TABLE ... DROP COLUMN`` step would delete
        existing data, unless the operator explicitly opts in via
        ``STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS=1``.

        Empty / non-existent tables and all-NULL / non-existent columns
        are dropped silently, as are the drops listed in
        ``_DATA_PRESERVING_DROPS`` — this guard only fires when real data
        would be lost. Each affected target produces one structured
        WARNING log line so operators can see exactly what they'd lose
        before opting in. ``--`` comments are ignored.
        """
        sql = _SQL_LINE_COMMENT_RE.sub("", sql)
        existing = set(self.tables())
        opt_in = os.environ.get(_DESTRUCTIVE_OPT_IN_ENV, "").lower() in {"1", "true", "yes"}

        def check(target: str, what: str, count_sql: str, unit: str) -> None:
            if (name, target) in _DATA_PRESERVING_DROPS:
                return
            count = int(self.con.execute(count_sql).fetchone()[0])
            if count == 0:
                return
            log.warning(
                "lake.migrate.destructive_drop",
                migration_version=version,
                table=target,
                rows=count,
                opt_in_env=_DESTRUCTIVE_OPT_IN_ENV,
            )
            if not opt_in:
                raise RuntimeError(
                    f"migration {version:03d} would {what} {target} which holds "
                    f"{count} {unit}; set {_DESTRUCTIVE_OPT_IN_ENV}=1 to confirm "
                    f"that the data is expendable, then re-run."
                )

        for match in _DROP_TABLE_RE.finditer(sql):
            table = match.group(1)
            if table in existing:
                check(table, "DROP TABLE", f"SELECT COUNT(*) FROM {table}", "row(s)")
        for match in _DROP_COLUMN_RE.finditer(sql):
            table, column = match.group(1), match.group(2)
            if table in existing and column in self._column_types(table):
                check(
                    f"{table}.{column}",
                    "DROP COLUMN",
                    f"SELECT COUNT({column}) FROM {table}",
                    "non-NULL value(s)",
                )

    def applied_migrations(self) -> list[int]:
        return [
            row[0]
            for row in self.con.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

    def tables(self) -> list[str]:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
        return [r[0] for r in rows]

    def count_rows(self, table: str) -> int:
        # Whitelist the name against the schema to keep this method safe to
        # call with caller-supplied strings (CLI args, config, etc.).
        known = set(self.tables())
        if table not in known:
            raise ValueError(f"unknown table {table!r}")
        return int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group multiple writes into one atomic unit.

        DuckDB's UPSERT and DELETE are statement-level atomic, but a
        sequence of them isn't — so any caller that wants "all or nothing"
        across several methods (e.g. an ingest bundle, or the
        delete-then-insert dance in ``upsert_officers``) must wrap the
        block in this helper. On exception the transaction is ROLLed BACK
        and the exception re-raises; on clean exit it COMMITs.

        Re-entrant: a nested call is a no-op (DuckDB doesn't support nested
        transactions, and the outer ``transaction()`` already owns the
        atomicity). The outermost caller is responsible for the
        BEGIN/COMMIT pair.
        """
        if getattr(self, "_in_transaction", False):
            # Inner caller piggy-backs on the outer transaction. The
            # outermost ``transaction()`` keeps the rollback responsibility.
            yield
            return
        self.con.execute("BEGIN")
        self._in_transaction = True
        try:
            yield
        except BaseException:
            # BaseException on purpose: after Ctrl-C an open DuckDB
            # transaction would make the next ``transaction()`` fail
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")
        finally:
            self._in_transaction = False

    # ---- bars (interval-aware) ---------------------------------------------

    def upsert_bars(self, df: pd.DataFrame, interval: Interval) -> int:
        """Upsert OHLCV bars at the given Interval.

        Expects columns ``ticker, timestamp, open, high, low, close,
        adj_close, volume``. The ``interval`` dimension is injected from
        the argument (not read from the frame) so callers can't silently
        mix granularities inside one batch. Last write wins per
        ``(ticker, timestamp, interval)``, in whichever bar store the lake
        uses.
        """
        if df.empty:
            return 0
        required = ("ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume")
        frame = df[list(required)].copy()
        frame["interval"] = interval.code
        return self._bars.upsert(frame[list(_BAR_COLS)])

    def get_bars(
        self,
        ticker: str,
        interval: Interval,
        start: Any,
        end: Any,
    ) -> pd.DataFrame:
        """Fetch bars at the given interval inside a ``[start, end]`` window."""
        return self._bars.get(ticker, interval, start, end)

    def delete_bars(self, ticker: str, interval: Interval, timestamps: list[Any]) -> int:
        """Remove the bars of one series at ``timestamps`` (naive UTC), for
        example a stored bar the quality checker found to be a spike.
        Returns how many were removed."""
        return self._bars.delete(ticker, interval, timestamps)

    def aggregate_bars(
        self,
        ticker: str,
        source: Interval,
        target: Interval,
    ) -> int:
        """Derive ``target``-interval bars for ``ticker`` by time-bucketing
        the already-stored ``source`` bars (open = first, close = last,
        high = max, low = min, volume = sum, ``adj_close`` = the last
        source bar's ``adj_close``, so the adjustment carries over). Idempotent:
        re-running overwrites the target bars with the current aggregation
        of source bars. Returns the net number of new target rows.

        Target must be a strictly coarser interval than source (the whole
        point of aggregation is upsampling duration).
        """
        if target.seconds <= source.seconds:
            raise ValueError(
                f"target interval {target.code} must be coarser than source {source.code}"
            )
        return self._bars.aggregate(ticker, source, target)

    # ---- prices (back-compat shim over daily bars) -------------------------

    def upsert_prices(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        frame = df[list(_PRICE_COLS)].copy()
        # Map the daily ``date`` column onto the ``timestamp`` column in bars.
        frame["timestamp"] = pd.to_datetime(frame["date"])
        frame = frame.drop(columns=["date"])
        return self.upsert_bars(frame, interval=Interval.DAY_1)

    def get_prices(self, ticker: str, start: Any, end: Any) -> pd.DataFrame:
        # widen ``date``-typed args to timestamp bounds so daily bars stored
        # at midnight fall inside the window.
        start_ts = day_start(start)
        end_ts = day_end(end)
        bars = self.get_bars(ticker, interval=Interval.DAY_1, start=start_ts, end=end_ts)
        if bars.empty:
            return pd.DataFrame(columns=list(_PRICE_COLS))
        out = bars.rename(columns={"timestamp": "date"})
        out["date"] = pd.to_datetime(out["date"]).dt.date
        return out[list(_PRICE_COLS)]

    # ---- financial statements (migration 008) ------------------------------

    def upsert_income_statement(self, df: pd.DataFrame) -> int:
        return self._upsert_statement(df, "income_statement", _INCOME_STATEMENT_COLS)

    def upsert_balance_sheet(self, df: pd.DataFrame) -> int:
        return self._upsert_statement(df, "balance_sheet", _BALANCE_SHEET_COLS)

    def upsert_cash_flow_statement(self, df: pd.DataFrame) -> int:
        return self._upsert_statement(df, "cash_flow_statement", _CASH_FLOW_STATEMENT_COLS)

    def _upsert_statement(self, df: pd.DataFrame, table: str, cols: tuple[str, ...]) -> int:
        """Upsert helper for the three wide financial-statement tables.

        Vendors omit line items that don't apply to a given filer (banks
        have no ``cost_of_revenue``, software firms no ``inventory``), so
        the input DataFrame is intentionally sparse. Two consequences:

        1. We reindex up to the full column set so absent columns land
           as NULL on first INSERT.
        2. On UPDATE conflict we use ``COALESCE(EXCLUDED.col, <table>.col)``
           so a NULL coming in from the input does **not** overwrite a
           prior non-NULL value. Real values overwrite (the typical
           "vendor restated revenue" case); absences are preserved.

        The PK columns must be present and non-NULL; the table's NOT
        NULL constraint catches NULL-valued PKs but the column-presence
        check raises a clearer error before that.
        """
        if df.empty:
            return 0
        missing_pk = [c for c in _STATEMENT_PK if c not in df.columns]
        if missing_pk:
            raise ValueError(
                f"{table}: missing required PK column(s) {missing_pk}; "
                f"input columns were {list(df.columns)}"
            )
        if df[list(_STATEMENT_PK)].isnull().to_numpy().any():
            raise ValueError(
                f"{table}: PK columns {_STATEMENT_PK} must be non-null; got NULL in input row(s)"
            )
        widened = df.reindex(columns=list(cols))
        return self._upsert_preserve_nulls(widened, table=table, cols=cols, pk=_STATEMENT_PK)

    def _upsert_preserve_nulls(
        self,
        df: pd.DataFrame,
        *,
        table: str,
        cols: tuple[str, ...],
        pk: tuple[str, ...],
    ) -> int:
        """Variant of :meth:`_upsert` that uses ``COALESCE(EXCLUDED.col,
        <table>.col)`` in the UPDATE clause so a NULL in the input does
        not overwrite a prior non-NULL value.

        Used by the wide statement upserts where "vendor omitted this
        line item" must not be confused with "vendor restated this line
        to NULL". For tables where the input row is always complete
        (e.g. ``dividends``, ``insider_transactions``), prefer the
        plain :meth:`_upsert` so explicit deletions can land.

        EXCLUDED is cast to each column's declared type so a sparse
        DataFrame (whose absent columns arrive as ``DOUBLE`` NaN after
        ``reindex``) can be coalesced against ``DATE`` / ``VARCHAR``
        columns without DuckDB's strict-type binder rejecting the mix.
        """
        if df.empty:
            return 0
        col_types = self._column_types(table)
        non_pk = [c for c in cols if c not in pk]
        update_clause = ", ".join(
            f"{c} = COALESCE(CAST(EXCLUDED.{c} AS {col_types[c]}), {table}.{c})" for c in non_pk
        )
        with self._registered(_last_per_key(df[list(cols)], pk)):
            self.con.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"SELECT {', '.join(cols)} FROM _in "
                f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {update_clause}"
            )
        return len(df)

    def _column_types(self, table: str) -> dict[str, str]:
        """``{column → declared type}`` for ``table``, cached per table.

        The schema only changes through ``migrate()``, which clears the
        cache after every migration it applies.
        """
        cached = self._column_type_cache.get(table)
        if cached is None:
            rows = self.con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='main' AND table_name = ?",
                [table],
            ).fetchall()
            cached = {row[0]: row[1] for row in rows}
            self._column_type_cache[table] = cached
        return cached

    def get_income_statement(self, ticker: str) -> pd.DataFrame:
        return self._get_statement("income_statement", ticker)

    def get_balance_sheet(self, ticker: str) -> pd.DataFrame:
        return self._get_statement("balance_sheet", ticker)

    def get_cash_flow_statement(self, ticker: str) -> pd.DataFrame:
        return self._get_statement("cash_flow_statement", ticker)

    def _get_statement(self, table: str, ticker: str) -> pd.DataFrame:
        return self.con.execute(
            f"SELECT * FROM {table} WHERE ticker = ? ORDER BY period_end DESC, frequency",
            [ticker],
        ).fetchdf()

    # ---- point-in-time reads (statements, macro) ----------------------------

    def get_statement_history(
        self,
        statement: str,
        ticker: str,
        *,
        missing_filing_lag_days: int = 90,
    ) -> pd.DataFrame:
        """Every row of one statement table for ``ticker``, oldest period
        first, plus an ``available_date`` column: the first date the row may
        be used without look-ahead.

        ``available_date`` is the day after the ``filing_date``: a filing
        may land after the close, so the first session that may act on it
        is the next one (P12, BE-22). When the vendor didn't supply a
        filing date it is ``period_end + missing_filing_lag_days`` (a
        conservative stand-in for the filing delay). It is never earlier
        than ``period_end``: a filing date before the period closed is bad
        data and is clamped.

        Known limit: a restatement that overwrites a row's values in place
        but keeps the original ``filing_date`` makes the restated numbers
        look available from the original date.
        """
        table = _statement_table(statement)
        lag = _non_negative_lag(missing_filing_lag_days)
        df = self.con.execute(
            f"""
            SELECT *,
                   CAST(GREATEST(COALESCE(filing_date + INTERVAL 1 DAY,
                                          period_end + to_days(CAST(? AS INTEGER))),
                                 period_end) AS DATE) AS available_date
              FROM {table}
             WHERE ticker = ?
             ORDER BY period_end, frequency
            """,
            [lag, ticker],
        ).fetchdf()
        return _dates_to_python(df, ("period_end", "filing_date", "available_date"))

    def get_statements_as_of(
        self,
        statement: str,
        ticker: str,
        as_of: Any,
        *,
        frequency: str | None = None,
        missing_filing_lag_days: int = 90,
        exclude_flagged: bool = False,
    ) -> pd.DataFrame:
        """Rows of :meth:`get_statement_history` whose ``available_date`` is
        on or before ``as_of`` (a date, or a datetime's calendar day),
        newest period first. ``frequency`` (``'Q'`` / ``'A'``) narrows the
        result to one reporting cadence. ``exclude_flagged`` drops periods
        the statement audit (BL-36) flagged with severity ``error``."""
        df = self.get_statement_history(
            statement, ticker, missing_filing_lag_days=missing_filing_lag_days
        )
        cutoff = _as_calendar_date(as_of)
        mask = df["available_date"].map(lambda d: d <= cutoff).astype(bool)
        if frequency is not None:
            mask &= df["frequency"] == frequency
        if exclude_flagged and not df.empty:
            flags = self.get_statement_flags(ticker, severity="error")
            bad = set(zip(flags["period_end"], flags["frequency"], strict=True))
            keys = zip(df["period_end"], df["frequency"], strict=True)
            mask &= pd.Series([k not in bad for k in keys], index=df.index)
        out = df[mask].iloc[::-1]
        return out.reset_index(drop=True)

    def get_macro_series(
        self,
        country_iso: str,
        indicator: str,
        *,
        as_of: Any = None,
        publication_lag_days: int = 0,
        stamped_at: str = "period_start",
    ) -> pd.DataFrame:
        """One macro indicator series (``observation_date, period, value,
        available_date``), oldest first.

        An observation can't be known before the period it describes is
        over, and is published some time after that:
        ``available_date = period end + publication_lag_days``.

        ``stamped_at`` says which end of its period ``observation_date``
        marks. The default, ``"period_start"``, matches vendors (EODHD) that
        stamp full-year 2023 as 2023-01-01; the period end is then derived
        from ``period`` (``annual`` / ``quarterly`` / ``monthly``, and a
        year when unknown, the conservative choice). ``"period_end"`` uses
        ``observation_date`` itself. With ``as_of`` set, only observations
        available on or before it are returned.
        """
        lag = _non_negative_lag(publication_lag_days)
        if stamped_at == "period_start":
            period_end = """CASE period
                    WHEN 'monthly' THEN observation_date + INTERVAL 1 MONTH
                    WHEN 'quarterly' THEN observation_date + INTERVAL 3 MONTH
                    ELSE observation_date + INTERVAL 1 YEAR
                END - INTERVAL 1 DAY"""
        elif stamped_at == "period_end":
            period_end = "observation_date"
        else:
            raise ValueError(
                f"stamped_at must be 'period_start' or 'period_end', got {stamped_at!r}"
            )
        df = self.con.execute(
            f"""
            SELECT observation_date, period, value,
                   CAST(CAST({period_end} AS DATE) + to_days(CAST(? AS INTEGER)) AS DATE)
                       AS available_date
              FROM macro_indicators
             WHERE country_iso = ? AND indicator = ?
             ORDER BY observation_date
            """,
            [lag, country_iso, indicator],
        ).fetchdf()
        df = _dates_to_python(df, ("observation_date", "available_date"))
        if as_of is not None:
            cutoff = _as_calendar_date(as_of)
            df = df[df["available_date"].map(lambda d: d <= cutoff).astype(bool)]
            df = df.reset_index(drop=True)
        return df

    def get_shares_outstanding(self, ticker: str) -> pd.DataFrame:
        """Share-count history (``date, shares``) for ``ticker``, oldest
        first. ``date`` is the date the count describes, not when it was
        published; point-in-time callers must add their own lag."""
        df = self.con.execute(
            "SELECT date, shares FROM shares_outstanding WHERE ticker = ? ORDER BY date",
            [ticker],
        ).fetchdf()
        return _dates_to_python(df, ("date",))

    # ---- statement audit (BL-36, migration 014) -----------------------------

    def replace_statement_flags(
        self, flags: pd.DataFrame, *, tickers: list[str] | None = None
    ) -> int:
        """Replace the ``statement_flags`` of ``tickers`` (every ticker when
        ``None``) with ``flags`` in one transaction. Written by
        :func:`stonks.store.audit.audit_statements`."""
        cols = (
            "ticker",
            "period_end",
            "frequency",
            "check_id",
            "severity",
            "detail",
            "flagged_at",
        )
        with self.transaction():
            if tickers is None:
                self.con.execute("DELETE FROM statement_flags")
            elif tickers:
                self.con.execute("DELETE FROM statement_flags WHERE ticker = ANY(?)", [tickers])
            if not flags.empty:
                self._upsert(
                    flags,
                    table="statement_flags",
                    cols=cols,
                    pk=("ticker", "period_end", "frequency", "check_id"),
                )
        return len(flags)

    # ---- data coverage (BL-37 lab preflight) ---------------------------------

    def bar_coverage(
        self, tickers: list[str], interval: Interval, start: Any, end: Any
    ) -> pd.DataFrame:
        """Per ticker with bars of ``interval`` up to ``end``: ``first_bar``
        and ``last_bar`` inside ``[start, end]`` (NULL when none),
        ``n_window`` bars inside it and ``n_before`` bars before ``start``.
        Tickers with no bar up to ``end`` are absent."""
        if not tickers:
            return pd.DataFrame(columns=["ticker", "first_bar", "last_bar", "n_window", "n_before"])
        lo = day_start(_as_calendar_date(start))
        hi = day_end(_as_calendar_date(end))
        return self.con.execute(
            """
            SELECT ticker,
                   MIN(timestamp) FILTER (WHERE timestamp >= ?) AS first_bar,
                   MAX(timestamp) FILTER (WHERE timestamp >= ?) AS last_bar,
                   COUNT(*) FILTER (WHERE timestamp >= ?) AS n_window,
                   COUNT(*) FILTER (WHERE timestamp < ?) AS n_before
              FROM bars
             WHERE ticker = ANY(?) AND interval = ? AND timestamp <= ?
             GROUP BY ticker ORDER BY ticker
            """,
            [lo, lo, lo, lo, list(tickers), str(interval), hi],
        ).fetchdf()

    def quarantine_counts(
        self, tickers: list[str], interval: Interval, start: Any, end: Any
    ) -> dict[str, int]:
        """``{ticker: quarantined bar count}`` inside ``[start, end]`` for
        ``interval`` (tickers with none are absent)."""
        if not tickers:
            return {}
        rows = self.con.execute(
            """
            SELECT ticker, COUNT(*) FROM quarantined_bars
             WHERE ticker = ANY(?) AND interval = ? AND timestamp BETWEEN ? AND ?
             GROUP BY ticker
            """,
            [
                list(tickers),
                str(interval),
                day_start(_as_calendar_date(start)),
                day_end(_as_calendar_date(end)),
            ],
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def delisted_tickers(self, tickers: list[str]) -> list[str]:
        """The ``tickers`` whose instrument profile marks them delisted."""
        if not tickers:
            return []
        rows = self.con.execute(
            """
            SELECT id FROM instruments
             WHERE id = ANY(?) AND (COALESCE(is_delisted, FALSE) OR delisted_date IS NOT NULL)
             ORDER BY id
            """,
            [list(tickers)],
        ).fetchall()
        return [r[0] for r in rows]

    # ---- universe membership (BL-37, migration 015) --------------------------

    _UNIVERSE_MEMBERSHIP_COLS = ("universe_id", "ticker", "start_date", "end_date")

    def upsert_universe_membership(self, df: pd.DataFrame) -> int:
        """Upsert membership spans keyed on ``(universe_id, ticker,
        start_date)``. ``end_date`` (optional column, NULL for an open span)
        is the first day the ticker is no longer a member."""
        if df.empty:
            return 0
        df = df.reindex(columns=list(self._UNIVERSE_MEMBERSHIP_COLS))
        start = pd.to_datetime(df["start_date"])
        end = pd.to_datetime(df["end_date"])
        bad = end.notna() & (end <= start)
        if bad.any():
            rows = df[bad][["ticker", "start_date", "end_date"]].to_dict("records")
            raise ValueError(f"universe_membership: end_date must be after start_date: {rows}")
        df = df.assign(
            start_date=[d.date() for d in start],
            end_date=[None if pd.isna(d) else d.date() for d in end],
        )
        return self._upsert(
            df,
            table="universe_membership",
            cols=self._UNIVERSE_MEMBERSHIP_COLS,
            pk=("universe_id", "ticker", "start_date"),
        )

    def members_as_of(self, universe_id: str, as_of: Any) -> list[str]:
        """Tickers in ``universe_id`` on ``as_of``, sorted."""
        return self.members_between(universe_id, as_of, as_of)

    def members_between(self, universe_id: str, start: Any, end: Any) -> list[str]:
        """Tickers in ``universe_id`` on at least one day of ``[start,
        end]``, sorted. Names that left or were delisted inside the window
        are included."""
        rows = self.con.execute(
            """
            SELECT DISTINCT ticker FROM universe_membership
             WHERE universe_id = ? AND start_date <= ?
               AND (end_date IS NULL OR end_date > ?)
             ORDER BY ticker
            """,
            [universe_id, _as_calendar_date(end), _as_calendar_date(start)],
        ).fetchall()
        return [r[0] for r in rows]

    def universe_ids(self) -> list[str]:
        """Every universe with at least one membership row, sorted."""
        rows = self.con.execute(
            "SELECT DISTINCT universe_id FROM universe_membership ORDER BY universe_id"
        ).fetchall()
        return [r[0] for r in rows]

    def get_universe_membership(
        self, universe_id: str | None = None, tickers: list[str] | None = None
    ) -> pd.DataFrame:
        """Membership spans, optionally narrowed to one universe or some
        tickers."""
        where, params = [], []
        if universe_id is not None:
            where.append("universe_id = ?")
            params.append(universe_id)
        if tickers is not None:
            where.append("ticker = ANY(?)")
            params.append(list(tickers))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        df = self.con.execute(
            f"SELECT * FROM universe_membership {clause} ORDER BY universe_id, ticker, start_date",
            params,
        ).fetchdf()
        return _dates_to_python(df, ("start_date", "end_date"))

    def get_statement_flags(
        self, ticker: str | None = None, *, severity: str | None = None
    ) -> pd.DataFrame:
        """Audit flags, one row per failing period and check, ordered by
        ticker, period and check. ``ticker`` and ``severity`` narrow it."""
        where, params = [], []
        if ticker is not None:
            where.append("ticker = ?")
            params.append(ticker)
        if severity is not None:
            where.append("severity = ?")
            params.append(severity)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        df = self.con.execute(
            f"SELECT * FROM statement_flags {clause} ORDER BY ticker, period_end, frequency, check_id",
            params,
        ).fetchdf()
        return _dates_to_python(df, ("period_end",))

    def get_bond_yields(self, ticker: str, *, as_of: Any = None) -> pd.DataFrame:
        """Yield history (``date, yield_to_maturity, clean_price``) for a
        bond ``ticker``, oldest first. With ``as_of`` set, only rows dated
        on or before its calendar day."""
        sql = "SELECT date, yield_to_maturity, clean_price FROM bond_yield_history WHERE ticker = ?"
        args: list[Any] = [ticker]
        if as_of is not None:
            sql += " AND date <= ?"
            args.append(_as_calendar_date(as_of))
        df = self.con.execute(sql + " ORDER BY date", args).fetchdf()
        return _dates_to_python(df, ("date",))

    # ---- ingest_runs --------------------------------------------------------

    def open_ingest_run(self, source: str, kind: str) -> int:
        row = self.con.execute(
            "INSERT INTO ingest_runs (source, kind, started_at, status) "
            "VALUES (?, ?, ?, 'running') RETURNING id",
            [source, kind, datetime.now(UTC)],
        ).fetchone()
        return int(row[0])

    def close_ingest_run(
        self,
        run_id: int,
        tickers_ok: int,
        tickers_failed: int,
        status: str,
        error: str | None = None,
    ) -> None:
        self.con.execute(
            """
            UPDATE ingest_runs
               SET finished_at = ?, tickers_ok = ?, tickers_failed = ?, status = ?, error = ?
             WHERE id = ?
            """,
            [datetime.now(UTC), tickers_ok, tickers_failed, status, error, run_id],
        )

    # ---- extended fundamentals (migrations 002 / 004 / 005) ----------------

    _DIVIDEND_COLS = (
        "ticker",
        "ex_date",
        "amount",
        "currency",
        "pay_date",
        "record_date",
        "declaration_date",
    )
    _INSIDER_COLS = (
        "ticker",
        "transaction_date",
        "filing_date",
        "owner_name",
        "owner_cik",
        "owner_relation",
        "owner_title",
        "transaction_code",
        "acquired_disposed",
        "shares",
        "price",
        "value",
        "post_transaction_amount",
        "sec_link",
    )
    _INSIDER_NATURAL_KEY = (
        "ticker",
        "transaction_date",
        "owner_name",
        "transaction_code",
        "shares",
        "sec_link",
    )
    _NEWS_COLS = (
        "ticker",
        "published_at",
        "title",
        "url",
        "source_name",
        "content",
        "symbols",
        "tags",
        "sentiment",
        "sentiment_pos",
        "sentiment_neg",
        "sentiment_neu",
    )
    _NEWS_SENTIMENT_COLS = ("ticker", "date", "sentiment", "article_count")
    _ANALYST_RATINGS_COLS = (
        "ticker",
        "snapshot_date",
        "target_price",
        "strong_buy",
        "buy",
        "hold",
        "sell",
        "strong_sell",
    )
    _ANALYST_RATINGS_VALUE_COLS = (
        "target_price",
        "strong_buy",
        "buy",
        "hold",
        "sell",
        "strong_sell",
    )
    _SHARES_OUT_COLS = ("ticker", "date", "shares")
    _EMPLOYEES_COLS = ("ticker", "date", "count")
    _SEGMENTATION_COLS = ("ticker", "period_end", "dimension", "segment", "value")
    _STOCK_SPLITS_COLS = ("ticker", "date", "ratio")
    _MARKET_CAP_COLS = ("ticker", "date", "market_cap")
    _INSTRUMENT_PROFILE_COLS = (
        "id",
        "asset_class",
        "exchange",
        "currency",
        "name",
        "country_iso",
        "sector",
        "industry",
        "gic_sector",
        "gic_group",
        "gic_industry",
        "gic_sub_industry",
        "ipo_date",
        "is_delisted",
        "delisted_date",
        "is_bank",
        "fiscal_year_end",
        "security_type",
        "cusip",
        "cik",
        "isin",
        "open_figi",
        "lei",
        "employer_id_number",
        "primary_ticker",
        "address_street",
        "address_city",
        "address_state",
        "address_country",
        "address_zip",
        "phone",
        "web_url",
        "description",
        "updated_at",
    )
    _INSTITUTIONAL_HOLDERS_COLS = (
        "ticker",
        "holder_kind",
        "name",
        "snapshot_date",
        "total_shares_pct",
        "total_assets_pct",
        "current_shares",
        "change_shares",
        "change_pct",
    )
    _INSTITUTIONAL_HOLDERS_VALUE_COLS = (
        "total_shares_pct",
        "total_assets_pct",
        "current_shares",
        "change_shares",
        "change_pct",
    )
    _EARNINGS_ANNOUNCEMENTS_COLS = (
        "ticker",
        "period_end",
        "report_date",
        "before_after_market",
        "currency",
        "eps_actual",
        "eps_estimate",
        "eps_difference",
        "surprise_percent",
    )
    _ANALYST_FORECASTS_COLS = (
        "ticker",
        "period_end",
        "period_relative",
        "growth",
        "eps_estimate_avg",
        "eps_estimate_low",
        "eps_estimate_high",
        "eps_estimate_year_ago",
        "eps_estimate_n_analysts",
        "eps_estimate_growth",
        "revenue_estimate_avg",
        "revenue_estimate_low",
        "revenue_estimate_high",
        "revenue_estimate_year_ago",
        "revenue_estimate_n_analysts",
        "revenue_estimate_growth",
        "eps_trend_current",
        "eps_trend_7d_ago",
        "eps_trend_30d_ago",
        "eps_trend_60d_ago",
        "eps_trend_90d_ago",
        "eps_revisions_up_7d",
        "eps_revisions_up_30d",
        "eps_revisions_down_7d",
        "eps_revisions_down_30d",
    )
    _ESG_SNAPSHOTS_COLS = (
        "ticker",
        "rating_date",
        "total_esg",
        "total_esg_percentile",
        "environment_score",
        "environment_percentile",
        "social_score",
        "social_percentile",
        "governance_score",
        "governance_percentile",
        "controversy_level",
    )
    _ESG_SNAPSHOTS_VALUE_COLS = tuple(
        c for c in _ESG_SNAPSHOTS_COLS if c not in ("ticker", "rating_date")
    )
    _ESG_ACTIVITIES_COLS = ("ticker", "rating_date", "activity", "involvement")
    _CROSS_LISTINGS_COLS = ("ticker", "exchange", "exchange_code", "name")
    _OFFICERS_COLS = ("ticker", "name", "title", "year_born")
    _TICKER_SNAPSHOTS_COLS = (
        "ticker",
        "snapshot_date",
        "beta",
        "short_percent",
        "percent_insiders",
        "percent_institutions",
    )
    _TICKER_SNAPSHOTS_VALUE_COLS = (
        "beta",
        "short_percent",
        "percent_insiders",
        "percent_institutions",
    )
    _CRYPTO_PROFILE_COLS = (
        "ticker",
        "base_symbol",
        "quote_symbol",
        "blockchain",
        "consensus_type",
        "circulating_supply",
        "total_supply",
        "max_supply",
        "supply_snapshot_date",
    )
    _BOND_PROFILE_COLS = (
        "ticker",
        "issuer_name",
        "issuer_kind",
        "bond_kind",
        "coupon_rate",
        "coupon_frequency",
        "face_value",
        "currency",
        "issue_date",
        "maturity_date",
        "credit_rating",
    )
    _BOND_YIELD_COLS = ("ticker", "date", "yield_to_maturity", "clean_price")
    _COMMODITY_CONTRACT_COLS = (
        "ticker",
        "underlying_symbol",
        "contract_kind",
        "contract_month",
        "expiry_date",
        "contract_size",
        "contract_unit",
    )
    _DEFI_TVL_COLS = ("chain", "observation_date", "tvl_usd", "source")
    _FX_RATE_COLS = ("base_currency", "quote_currency", "observation_date", "rate", "source")
    _MACRO_INDICATOR_COLS = (
        "country_iso",
        "indicator",
        "observation_date",
        "period",
        "country_name",
        "value",
    )

    def upsert_dividends(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="dividends",
            cols=self._DIVIDEND_COLS,
            pk=("ticker", "ex_date"),
        )

    def get_dividends(self, ticker: str) -> pd.DataFrame:
        return self.con.execute(
            "SELECT * FROM dividends WHERE ticker = ? ORDER BY ex_date",
            [ticker],
        ).fetchdf()

    def get_corporate_actions(self, tickers: list[str]) -> pd.DataFrame:
        """Splits and cash dividends for ``tickers`` in one query.

        Columns ``ticker, ex_date, kind, value``: ``kind`` is ``split``
        (``value`` = new shares per old share) or ``dividend`` (``value`` =
        cash per share on the ex-date). Ordered by ticker, ex-date, then
        splits before dividends on the same ex-date."""
        if not tickers:
            return pd.DataFrame(columns=["ticker", "ex_date", "kind", "value"])
        df = self.con.execute(
            """
            SELECT ticker, ex_date, kind, value FROM (
                SELECT ticker, date AS ex_date, 'split' AS kind, ratio AS value, 0 AS ord
                  FROM stock_splits WHERE ticker = ANY(?)
                UNION ALL
                SELECT ticker, ex_date, 'dividend' AS kind, amount AS value, 1 AS ord
                  FROM dividends WHERE ticker = ANY(?)
            )
            ORDER BY ticker, ex_date, ord
            """,
            [list(tickers), list(tickers)],
        ).fetchdf()
        return _dates_to_python(df, ("ex_date",))

    def upsert_insider_transactions(self, df: pd.DataFrame) -> int:
        # Deduplicates on the NULL-safe ``natural_key`` column (migration
        # 010) derived from _INSIDER_NATURAL_KEY; the synthetic id column
        # is excluded from insert.
        return self._upsert(
            df,
            table="insider_transactions",
            cols=self._INSIDER_COLS,
            pk=self._INSIDER_NATURAL_KEY,
            natural_key_sql=_INSIDER_NATURAL_KEY_SQL,
        )

    def upsert_news(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="news",
            cols=self._NEWS_COLS,
            pk=("ticker", "published_at", "title"),
        )

    def upsert_news_sentiment(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="news_sentiment",
            cols=self._NEWS_SENTIMENT_COLS,
            pk=("ticker", "date"),
        )

    def upsert_analyst_ratings(self, df: pd.DataFrame) -> int:
        # Time-series + change-detection: only insert a new row when the
        # consensus actually moves from the most recent prior snapshot.
        return self._upsert_on_change(
            df,
            table="analyst_ratings",
            cols=list(self._ANALYST_RATINGS_COLS),
            identity_cols=["ticker"],
            value_cols=list(self._ANALYST_RATINGS_VALUE_COLS),
            snapshot_col="snapshot_date",
        )

    def upsert_shares_outstanding(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="shares_outstanding",
            cols=self._SHARES_OUT_COLS,
            pk=("ticker", "date"),
        )

    def upsert_employee_count(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="employee_count",
            cols=self._EMPLOYEES_COLS,
            pk=("ticker", "date"),
        )

    def upsert_segmentation(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="segmentation",
            cols=self._SEGMENTATION_COLS,
            pk=("ticker", "period_end", "dimension", "segment"),
        )

    def upsert_stock_splits(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="stock_splits",
            cols=self._STOCK_SPLITS_COLS,
            pk=("ticker", "date"),
        )

    def upsert_market_cap_history(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="market_cap_history",
            cols=self._MARKET_CAP_COLS,
            pk=("ticker", "date"),
        )

    def upsert_instrument_profile(self, df: pd.DataFrame) -> int:
        """Profiles arrive from several sources of differing richness (EODHD
        carries ISIN/CIK/IPO date, Yahoo doesn't), so a NULL or absent field
        from a later source keeps the earlier source's value instead of
        clearing it. Real values still overwrite."""
        if df.empty:
            return 0
        return self._upsert_preserve_nulls(
            df.reindex(columns=list(self._INSTRUMENT_PROFILE_COLS)),
            table="instruments",
            cols=self._INSTRUMENT_PROFILE_COLS,
            pk=("id",),
        )

    def upsert_crypto_profile(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="crypto_profiles",
            cols=self._CRYPTO_PROFILE_COLS,
            pk=("ticker",),
        )

    def upsert_bond_profile(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="bond_profiles",
            cols=self._BOND_PROFILE_COLS,
            pk=("ticker",),
        )

    def upsert_bond_yields(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="bond_yield_history",
            cols=self._BOND_YIELD_COLS,
            pk=("ticker", "date"),
        )

    def upsert_commodity_contract(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="commodity_contracts",
            cols=self._COMMODITY_CONTRACT_COLS,
            pk=("ticker",),
        )

    def upsert_macro_indicators(self, df: pd.DataFrame) -> int:
        """Upsert macroeconomic-indicator observations.

        Identity is ``(country_iso, indicator, observation_date)``: each
        country × indicator × date pair maps to one observation. Re-running
        the same fetch is a no-op; vendor revisions to a previously-published
        value land via the ``ON CONFLICT DO UPDATE`` branch in plain
        ``_upsert`` (NULL still overwrites here — unlike financial
        statements, the macro endpoint always returns the full series, so
        a NULL ``Value`` is the vendor's authoritative "not published"
        rather than "not present in this fetch").
        """
        return self._upsert(
            df,
            table="macro_indicators",
            cols=self._MACRO_INDICATOR_COLS,
            pk=("country_iso", "indicator", "observation_date"),
        )

    def upsert_defi_tvl(self, df: pd.DataFrame) -> int:
        """Upsert daily DeFi TVL observations keyed by
        ``(chain, observation_date)``. Last write wins (``source`` and
        ``tvl_usd`` both), so re-running a fetch is a no-op and vendor
        revisions land in place."""
        return self._upsert(
            df,
            table="defi_tvl",
            cols=self._DEFI_TVL_COLS,
            pk=("chain", "observation_date"),
        )

    def get_defi_tvl(
        self,
        chain: str,
        *,
        start: Any = None,
        end: Any = None,
    ) -> pd.DataFrame:
        """One chain's TVL series (``observation_date, tvl_usd, source``),
        oldest first, optionally limited to ``start <= observation_date <=
        end`` (dates or datetimes' calendar days). ``observation_date`` is
        the vendor stamp, not a publication date: callers add their own
        availability lag."""
        clauses = ["chain = ?"]
        params: list[Any] = [chain]
        if start is not None:
            clauses.append("observation_date >= ?")
            params.append(_as_calendar_date(start))
        if end is not None:
            clauses.append("observation_date <= ?")
            params.append(_as_calendar_date(end))
        df = self.con.execute(
            "SELECT observation_date, tvl_usd, source FROM defi_tvl"
            f" WHERE {' AND '.join(clauses)} ORDER BY observation_date",
            params,
        ).fetchdf()
        return _dates_to_python(df, ("observation_date",))

    def upsert_fx_rates(self, df: pd.DataFrame) -> int:
        """Upsert daily FX rates keyed by ``(base_currency, quote_currency,
        observation_date)``. Last write wins, so a re-run is a no-op."""
        return self._upsert(
            df,
            table="fx_rates",
            cols=self._FX_RATE_COLS,
            pk=("base_currency", "quote_currency", "observation_date"),
        )

    def get_fx_rates(
        self,
        currencies: Any = None,
        *,
        end: Any = None,
    ) -> pd.DataFrame:
        """Stored FX rates (``base_currency, quote_currency,
        observation_date, rate``), oldest first. ``currencies`` keeps pairs
        whose both legs are in it; ``end`` keeps days on or before it."""
        clauses: list[str] = []
        params: list[Any] = []
        if currencies is not None:
            codes = sorted({str(c).upper() for c in currencies})
            clauses.append("base_currency = ANY(?) AND quote_currency = ANY(?)")
            params += [codes, codes]
        if end is not None:
            clauses.append("observation_date <= ?")
            params.append(_as_calendar_date(end))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        df = self.con.execute(
            "SELECT base_currency, quote_currency, observation_date, rate FROM fx_rates"
            f"{where} ORDER BY observation_date",
            params,
        ).fetchdf()
        return _dates_to_python(df, ("observation_date",))

    def upsert_institutional_holders(self, df: pd.DataFrame) -> int:
        return self._upsert_on_change(
            df,
            table="institutional_holders",
            cols=list(self._INSTITUTIONAL_HOLDERS_COLS),
            identity_cols=["ticker", "holder_kind", "name"],
            value_cols=list(self._INSTITUTIONAL_HOLDERS_VALUE_COLS),
            snapshot_col="snapshot_date",
        )

    def upsert_earnings_announcements(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="earnings_announcements",
            cols=self._EARNINGS_ANNOUNCEMENTS_COLS,
            pk=("ticker", "period_end"),
        )

    def upsert_analyst_forecasts(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="analyst_forecasts",
            cols=self._ANALYST_FORECASTS_COLS,
            pk=("ticker", "period_end", "period_relative"),
        )

    def upsert_esg_snapshots(self, df: pd.DataFrame) -> int:
        return self._upsert_on_change(
            df,
            table="esg_snapshots",
            cols=list(self._ESG_SNAPSHOTS_COLS),
            identity_cols=["ticker"],
            value_cols=list(self._ESG_SNAPSHOTS_VALUE_COLS),
            snapshot_col="rating_date",
        )

    def upsert_esg_activities(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="esg_activities",
            cols=self._ESG_ACTIVITIES_COLS,
            pk=("ticker", "rating_date", "activity"),
        )

    def upsert_cross_listings(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="cross_listings",
            cols=self._CROSS_LISTINGS_COLS,
            pk=("ticker", "exchange", "exchange_code"),
        )

    def upsert_officers(self, df: pd.DataFrame) -> int:
        """Replace the officer roster for each distinct ticker in ``df``.

        Officers don't carry per-officer dates from the vendor, so the table
        is current-state, not time-series. For each ticker present in the
        input, existing rows are deleted before the new roster is inserted —
        so departures show up as removed rows on the next fetch.
        """
        if df.empty:
            return 0
        frame = df[list(self._OFFICERS_COLS)]
        tickers = frame["ticker"].unique().tolist()
        if not tickers:
            return 0
        # Atomic delete-then-insert: if the INSERT fails (e.g. a NOT
        # NULL violation on a column the vendor unexpectedly returned
        # blank), we don't want the DELETE to have already wiped the
        # roster — wrap both statements in one transaction so the
        # caller either sees the new roster or the old one, never an
        # empty one.
        with self._registered(_last_per_key(frame, ("ticker", "name"))), self.transaction():
            placeholders = ",".join(["?"] * len(tickers))
            self.con.execute(
                f"DELETE FROM officers WHERE ticker IN ({placeholders})",
                tickers,
            )
            self.con.execute(
                f"INSERT INTO officers ({', '.join(self._OFFICERS_COLS)}) "
                f"SELECT {', '.join(self._OFFICERS_COLS)} FROM _in"
            )
        return len(df)

    def upsert_ticker_snapshots(self, df: pd.DataFrame) -> int:
        return self._upsert_on_change(
            df,
            table="ticker_snapshots",
            cols=list(self._TICKER_SNAPSHOTS_COLS),
            identity_cols=["ticker"],
            value_cols=list(self._TICKER_SNAPSHOTS_VALUE_COLS),
            snapshot_col="snapshot_date",
        )

    # ---- generic upsert helpers --------------------------------------------

    def _upsert(
        self,
        df: pd.DataFrame,
        *,
        table: str,
        cols: tuple[str, ...],
        pk: tuple[str, ...],
        natural_key_sql: str | None = None,
    ) -> int:
        """Last-write-wins upsert of ``cols`` keyed on ``pk``.

        With ``natural_key_sql``, the table's conflict target is a
        ``natural_key`` column computed from ``pk`` by that SQL expression
        (used where ``pk`` columns are nullable and a plain unique index
        would treat NULLs as distinct); ``pk`` still drives in-batch
        dedup and which columns are left alone on UPDATE.
        """
        if df.empty:
            return 0
        non_pk = [c for c in cols if c not in pk]
        if non_pk:
            action = "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
        else:
            action = "DO NOTHING"
        insert_cols = ", ".join(cols)
        select_cols = insert_cols
        conflict = ", ".join(pk)
        if natural_key_sql is not None:
            insert_cols += ", natural_key"
            select_cols += f", {natural_key_sql}"
            conflict = "natural_key"
        with self._registered(_last_per_key(df[list(cols)], pk)):
            self.con.execute(
                f"INSERT INTO {table} ({insert_cols}) "
                f"SELECT {select_cols} FROM _in "
                f"ON CONFLICT ({conflict}) {action}"
            )
        return len(df)

    @contextmanager
    def _registered(self, df: pd.DataFrame, name: str = "_in") -> Iterator[None]:
        """Expose ``df`` to SQL as the view ``name`` for the duration of
        the block, unregistering it even if the statement raises."""
        self.con.register(name, df)
        try:
            yield
        finally:
            self.con.unregister(name)

    def _upsert_on_change(
        self,
        df: pd.DataFrame,
        *,
        table: str,
        cols: list[str],
        identity_cols: list[str],
        value_cols: list[str],
        snapshot_col: str,
    ) -> int:
        """Insert-on-change helper for snapshot tables (SCD-2-lite).

        Three behaviours, in one call:

        1. **First time seeing this identity** (no prior row) → INSERT.
        2. **Same ``snapshot_col`` as the latest prior row** → UPDATE the
           existing row's value columns (handles vendor corrections).
        3. **Newer ``snapshot_col`` than the latest prior row** → INSERT a
           new row only if at least one ``value_col`` differs from the
           latest prior row; otherwise NO-OP (the polling cadence
           outpaced the data's real change cadence).

        Backfill of older snapshot dates is permitted: rows whose
        ``snapshot_col`` is ``<=`` the latest stored row always pass the
        eligibility filter. If the exact ``(identity_cols..., snapshot_col)``
        row already exists, the ON CONFLICT branch UPDATEs it in place
        (vendor correction); otherwise it INSERTs a new historical row.

        Returns the *net* number of rows added to ``table`` (count after −
        count before) — **not** rows touched. ON CONFLICT updates and
        no-change skips both return 0, indistinguishable from each other
        at this layer; callers that need to disambiguate should query
        ``table`` directly before/after.

        ``identity_cols`` define the entity (e.g. ``["ticker", "name"]``);
        ``value_cols`` are the observable columns we compare for change;
        the rest of ``cols`` get persisted on INSERT but aren't part of
        the change check.

        Tables backed by this helper must declare a PRIMARY KEY of
        ``(identity_cols..., snapshot_col)`` so the ON CONFLICT clause
        has something to fire on.

        A batch may hold several snapshots of one identity (a history
        backfill). Rows newer than the stored latest are then judged in
        date order, each against the last row kept, so one batch stores
        exactly what the same rows sent one poll at a time would.
        """
        if df.empty:
            return 0

        cols = list(cols)
        key = (*identity_cols, snapshot_col)
        batch = _last_per_key(df[cols], key)
        update_clause = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in (*identity_cols, snapshot_col)
        )
        col_list = ", ".join(cols)
        pk_list = ", ".join(key)

        with self.transaction():
            latest = self._latest_snapshots(table, batch, identity_cols, snapshot_col)
            eligible = _changed_snapshots(batch, latest, identity_cols, value_cols, snapshot_col)
            before = int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if not eligible.empty:
                with self._registered(eligible):
                    self.con.execute(
                        f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM _in "
                        f"ON CONFLICT ({pk_list}) DO UPDATE SET {update_clause}"
                    )
            after = int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return after - before

    def _latest_snapshots(
        self,
        table: str,
        batch: pd.DataFrame,
        identity_cols: list[str],
        snapshot_col: str,
    ) -> pd.DataFrame:
        """The stored latest row of every identity present in ``batch``."""
        ids = batch[identity_cols].drop_duplicates()
        in_batch = " AND ".join(f"_ids.{c} = {table}.{c}" for c in identity_cols)
        partition = ", ".join(identity_cols)
        with self._registered(ids, "_ids"):
            return self.con.execute(
                f"""
                SELECT * EXCLUDE (_rn) FROM (
                    SELECT *, ROW_NUMBER() OVER (
                               PARTITION BY {partition} ORDER BY {snapshot_col} DESC
                           ) AS _rn
                      FROM {table}
                     WHERE EXISTS (SELECT 1 FROM _ids WHERE {in_batch})
                ) WHERE _rn = 1
                """
            ).fetchdf()

    # ---- multi-asset helpers ------------------------------------------------

    def get_asset_classes(self, tickers: list[str]) -> dict[str, str]:
        """Return ``{ticker → asset_class}`` for every ticker that has a
        row in ``instruments``.

        Tickers with no instrument profile (price-bar landed before the
        profile fetch, or the profile fetch failed) are omitted from the
        result rather than defaulted — callers should treat the absence
        as "asset class unknown" and decide locally how to handle it.
        Used by the Ranker to drop universe tickers outside a strategy's
        ``applicable_asset_classes``.
        """
        if not tickers:
            return {}
        placeholders = ",".join(["?"] * len(tickers))
        rows = self.con.execute(
            f"SELECT id, asset_class FROM instruments "
            f"WHERE id IN ({placeholders}) AND asset_class IS NOT NULL",
            tickers,
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def bar_first_stamps(self, interval: Interval) -> dict[str, datetime]:
        """``{ticker: first bar timestamp}`` for every ticker with bars at
        ``interval`` (the point-in-time lake lists a name only once its
        first bar exists, BL-49)."""
        rows = self.sql(
            "SELECT ticker, MIN(timestamp) AS first FROM bars WHERE interval = ? GROUP BY ticker",
            [interval.code],
        )
        out: dict[str, datetime] = {}
        for t, ts in zip(rows["ticker"], rows["first"], strict=True):
            out[str(t)] = cast(datetime, pd.Timestamp(ts).to_pydatetime())
        return out

    def bar_tickers(self, interval: Interval) -> list[str]:
        """Tickers with bars at ``interval``, sorted."""
        return sorted(self.bar_first_stamps(interval))

    def instrument_sectors(self, tickers: list[str]) -> pd.DataFrame:
        """``id, sector, gic_sector`` of the ``instruments`` rows for
        ``tickers`` (static profile data, no time stamp)."""
        return self.sql(
            "SELECT id, sector, gic_sector FROM instruments WHERE id = ANY(?)", [list(tickers)]
        )

    # ---- escape hatch -------------------------------------------------------

    def sql(self, query: str, params: list | None = None) -> pd.DataFrame:
        cur = self.con.execute(query, params) if params else self.con.execute(query)
        return cur.fetchdf()
