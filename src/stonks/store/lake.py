"""DuckDB-backed analytical data lake: prices, financial statements,
extended metadata, ingest run ledger.

Schema lives in ``migrations_duckdb/*.sql``; versions are tracked in
``schema_migrations`` and applied in lexical order at ``migrate()`` time.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.timeutil import day_end, day_start
from stonks.logging import get_logger

MIGRATIONS_DIR = Path(__file__).parent / "migrations_duckdb"

# Env var that lets an operator opt into a destructive migration
# (``DROP TABLE``) against a table that currently holds rows. The default
# is to refuse — losing committed data must be an explicit choice, not
# something a routine ``stonks db init`` does silently.
_DESTRUCTIVE_OPT_IN_ENV = "STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS"
_DROP_TABLE_RE = re.compile(
    r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.IGNORECASE,
)

_PRICE_COLS = ("ticker", "date", "open", "high", "low", "close", "adj_close", "volume")
_BAR_COLS = (
    "ticker",
    "timestamp",
    "interval",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
)
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


class DuckDBLake:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con: duckdb.DuckDBPyConnection | None = duckdb.connect(str(self._path))
        # Pin the session to UTC so tz-aware inputs (e.g. UTC intraday
        # timestamps from EODHD) aren't silently shifted into the host's
        # local time when they land in naive-TIMESTAMP columns.
        self._con.execute("SET TimeZone = 'UTC'")

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
            sql = path.read_text()
            self._guard_destructive_drops(sql, version=version, log=log)
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

    def _guard_destructive_drops(self, sql: str, *, version: int, log: Any) -> None:
        """Refuse to apply a migration whose ``DROP TABLE`` step would
        delete a table that currently holds rows, unless the operator
        explicitly opts in via ``STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS=1``.

        Empty / non-existent tables are dropped silently — this guard
        only fires when real data would be lost. Each affected table
        produces one structured WARNING log line so operators can see
        exactly what they'd lose before opting in.
        """
        existing = set(self.tables())
        opt_in = os.environ.get(_DESTRUCTIVE_OPT_IN_ENV, "").lower() in {"1", "true", "yes"}
        for match in _DROP_TABLE_RE.finditer(sql):
            target = match.group(1)
            if target not in existing:
                continue
            row_count = int(self.con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0])
            if row_count == 0:
                continue
            log.warning(
                "lake.migrate.destructive_drop",
                migration_version=version,
                table=target,
                rows=row_count,
                opt_in_env=_DESTRUCTIVE_OPT_IN_ENV,
            )
            if not opt_in:
                raise RuntimeError(
                    f"migration {version:03d} would DROP TABLE {target} which holds "
                    f"{row_count} row(s); set {_DESTRUCTIVE_OPT_IN_ENV}=1 to confirm "
                    f"that the data is expendable, then re-run."
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
        except Exception:
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
        mix granularities inside one batch.
        """
        if df.empty:
            return 0
        required = ("ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume")
        frame = df[list(required)].copy()
        frame["interval"] = interval.code
        self.con.register("_in", frame[list(_BAR_COLS)])
        try:
            self.con.execute(
                """
                INSERT INTO bars (
                    ticker, timestamp, interval,
                    open, high, low, close, adj_close, volume)
                SELECT ticker, timestamp, interval,
                       open, high, low, close, adj_close, volume
                FROM _in
                ON CONFLICT (ticker, timestamp, interval) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    adj_close = EXCLUDED.adj_close,
                    volume = EXCLUDED.volume
                """
            )
        finally:
            self.con.unregister("_in")
        return len(df)

    def get_bars(
        self,
        ticker: str,
        interval: Interval,
        start: Any,
        end: Any,
    ) -> pd.DataFrame:
        """Fetch bars at the given interval inside a ``[start, end]`` window."""
        return self.con.execute(
            """
            SELECT ticker, timestamp, open, high, low, close, adj_close, volume
              FROM bars
             WHERE ticker = ? AND interval = ? AND timestamp BETWEEN ? AND ?
             ORDER BY timestamp
            """,
            [ticker, interval.code, start, end],
        ).fetchdf()

    def aggregate_bars(
        self,
        ticker: str,
        source: Interval,
        target: Interval,
    ) -> int:
        """Derive ``target``-interval bars for ``ticker`` by time-bucketing
        the already-stored ``source`` bars. Idempotent via the bars-table
        PK; re-running overwrites the target bars with the current
        aggregation of source bars.

        Target must be a strictly coarser interval than source (the whole
        point of aggregation is upsampling duration). Any prior target
        bars for this ticker fall under the ON CONFLICT path.
        """
        if target.seconds <= source.seconds:
            raise ValueError(
                f"target interval {target.code} must be coarser than source {source.code}"
            )
        # DuckDB's time_bucket(interval, ts) aligns on the interval origin.
        # OHLCV aggregation within each bucket:
        #   open  = first (earliest timestamp)
        #   close = last  (latest timestamp)
        #   high  = max, low = min
        #   volume = sum
        # adj_close tracks close (we don't have per-bucket corporate-actions
        # adjustment here; closing price is the best we can do).
        sql = f"""
            INSERT INTO bars (
                ticker, timestamp, interval,
                open, high, low, close, adj_close, volume)
            SELECT
                ticker,
                time_bucket({target.duckdb_interval}, timestamp) AS bucket,
                ? AS interval,
                arg_min(open, timestamp) AS open,
                max(high) AS high,
                min(low) AS low,
                arg_max(close, timestamp) AS close,
                arg_max(close, timestamp) AS adj_close,
                sum(volume) AS volume
              FROM bars
             WHERE ticker = ? AND interval = ?
             GROUP BY ticker, bucket
            ON CONFLICT (ticker, timestamp, interval) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                adj_close = EXCLUDED.adj_close,
                volume = EXCLUDED.volume
        """
        before = self.count_rows("bars")
        self.con.execute(sql, [target.code, ticker, source.code])
        return int(self.count_rows("bars") - before)

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
        self.con.register("_in", df[list(cols)])
        non_pk = [c for c in cols if c not in pk]
        update_clause = ", ".join(
            f"{c} = COALESCE(CAST(EXCLUDED.{c} AS {col_types[c]}), {table}.{c})" for c in non_pk
        )
        try:
            sql = (
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"SELECT {', '.join(cols)} FROM _in "
                f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {update_clause}"
            )
            self.con.execute(sql)
        finally:
            self.con.unregister("_in")
        return len(df)

    def _column_types(self, table: str) -> dict[str, str]:
        rows = self.con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name = ?",
            [table],
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def get_income_statement(self, ticker: str) -> pd.DataFrame:
        return self.con.execute(
            "SELECT * FROM income_statement WHERE ticker = ? ORDER BY period_end DESC, frequency",
            [ticker],
        ).fetchdf()

    def get_balance_sheet(self, ticker: str) -> pd.DataFrame:
        return self.con.execute(
            "SELECT * FROM balance_sheet WHERE ticker = ? ORDER BY period_end DESC, frequency",
            [ticker],
        ).fetchdf()

    def get_cash_flow_statement(self, ticker: str) -> pd.DataFrame:
        return self.con.execute(
            "SELECT * FROM cash_flow_statement "
            "WHERE ticker = ? ORDER BY period_end DESC, frequency",
            [ticker],
        ).fetchdf()

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

    def upsert_insider_transactions(self, df: pd.DataFrame) -> int:
        # Deduplicates on (ticker, transaction_date, owner_name,
        # transaction_code, shares, sec_link) via the unique index;
        # the synthetic id column is excluded from insert.
        if df.empty:
            return 0
        self.con.register("_in", df[list(self._INSIDER_COLS)])
        try:
            non_pk = (
                "filing_date",
                "owner_cik",
                "owner_relation",
                "owner_title",
                "acquired_disposed",
                "price",
                "value",
                "post_transaction_amount",
            )
            update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
            self.con.execute(
                f"""
                INSERT INTO insider_transactions ({", ".join(self._INSIDER_COLS)})
                SELECT {", ".join(self._INSIDER_COLS)} FROM _in
                ON CONFLICT (ticker, transaction_date, owner_name,
                             transaction_code, shares, sec_link)
                DO UPDATE SET {update_clause}
                """
            )
        finally:
            self.con.unregister("_in")
        return len(df)

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
        return self._upsert(
            df,
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
        self.con.register("_in", frame)
        try:
            # Atomic delete-then-insert: if the INSERT fails (e.g. a NOT
            # NULL violation on a column the vendor unexpectedly returned
            # blank), we don't want the DELETE to have already wiped the
            # roster — wrap both statements in one transaction so the
            # caller either sees the new roster or the old one, never an
            # empty one.
            with self.transaction():
                placeholders = ",".join(["?"] * len(tickers))
                self.con.execute(
                    f"DELETE FROM officers WHERE ticker IN ({placeholders})",
                    tickers,
                )
                self.con.execute(
                    f"INSERT INTO officers ({', '.join(self._OFFICERS_COLS)}) "
                    f"SELECT {', '.join(self._OFFICERS_COLS)} FROM _in"
                )
        finally:
            self.con.unregister("_in")
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
    ) -> int:
        if df.empty:
            return 0
        self.con.register("_in", df[list(cols)])
        non_pk = [c for c in cols if c not in pk]
        update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
        try:
            if non_pk:
                sql = (
                    f"INSERT INTO {table} ({', '.join(cols)}) "
                    f"SELECT {', '.join(cols)} FROM _in "
                    f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {update_clause}"
                )
            else:
                sql = (
                    f"INSERT INTO {table} ({', '.join(cols)}) "
                    f"SELECT {', '.join(cols)} FROM _in "
                    f"ON CONFLICT ({', '.join(pk)}) DO NOTHING"
                )
            self.con.execute(sql)
        finally:
            self.con.unregister("_in")
        return len(df)

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
        """
        if df.empty:
            return 0

        cols = list(cols)
        identity_eq = " AND ".join(f"_in.{c} = latest.{c}" for c in identity_cols)
        # NULL-drift suppression (I10): a vendor briefly returning NULL for
        # a previously-known value is *not* a change worth recording — that
        # would bloat the time series with vendor flakiness, not real
        # movement. So we count a column as "changed" only when the new
        # value is non-NULL AND differs from the prior. Going from NULL to
        # a real value still counts (we just learned it); going the other
        # way doesn't (we forgot it momentarily).
        any_value_changed = " OR ".join(
            (
                f"((latest.{c} IS NULL AND _in.{c} IS NOT NULL) "
                f"OR (latest.{c} IS NOT NULL AND _in.{c} IS NOT NULL "
                f"AND latest.{c} != _in.{c}))"
            )
            for c in value_cols
        )
        update_clause = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in (*identity_cols, snapshot_col)
        )
        partition = ", ".join(identity_cols)
        col_list = ", ".join(cols)
        pk_list = ", ".join((*identity_cols, snapshot_col))

        self.con.register("_in", df[cols])
        try:
            with self.transaction():
                before = int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                sql = f"""
                    WITH ranked AS (
                        SELECT *,
                               ROW_NUMBER() OVER (
                                   PARTITION BY {partition}
                                   ORDER BY {snapshot_col} DESC
                               ) AS _rn
                          FROM {table}
                    ),
                    latest AS (
                        SELECT * FROM ranked WHERE _rn = 1
                    ),
                    eligible AS (
                        SELECT _in.* FROM _in
                        LEFT JOIN latest ON {identity_eq}
                        WHERE
                            latest.{snapshot_col} IS NULL
                            OR _in.{snapshot_col} <= latest.{snapshot_col}
                            OR ({any_value_changed})
                    )
                    INSERT INTO {table} ({col_list})
                    SELECT {col_list} FROM eligible
                    ON CONFLICT ({pk_list})
                    DO UPDATE SET {update_clause}
                """
                self.con.execute(sql)
                after = int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                return after - before
        finally:
            self.con.unregister("_in")

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

    # ---- escape hatch -------------------------------------------------------

    def sql(self, query: str, params: list | None = None) -> pd.DataFrame:
        cur = self.con.execute(query, params) if params else self.con.execute(query)
        return cur.fetchdf()
