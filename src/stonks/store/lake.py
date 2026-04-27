"""DuckDB-backed analytical data lake: prices, fundamentals, ingest run ledger.

Schema lives in ``migrations/*.sql``; versions are tracked in ``schema_migrations``
and applied in lexical order at ``migrate()`` time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.timeutil import day_end, day_start

MIGRATIONS_DIR = Path(__file__).parent / "migrations_duckdb"

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
_FUND_COLS = ("ticker", "period_end", "frequency", "statement", "line_item", "value")


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
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.stem.split("_", 1)[0])
            if version in applied:
                continue
            sql = path.read_text()
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

    # ---- fundamentals -------------------------------------------------------

    def upsert_fundamentals(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        self.con.register("_in", df[list(_FUND_COLS)])
        try:
            self.con.execute(
                """
                INSERT INTO fundamentals (ticker, period_end, frequency, statement, line_item, value)
                SELECT ticker, period_end, frequency, statement, line_item, value FROM _in
                ON CONFLICT (ticker, period_end, frequency, statement, line_item) DO UPDATE SET
                    value = EXCLUDED.value
                """
            )
        finally:
            self.con.unregister("_in")
        return len(df)

    def get_fundamentals(self, ticker: str, statement: str | None = None) -> pd.DataFrame:
        if statement is None:
            return self.con.execute(
                "SELECT * FROM fundamentals WHERE ticker = ?", [ticker]
            ).fetchdf()
        return self.con.execute(
            "SELECT * FROM fundamentals WHERE ticker = ? AND statement = ?",
            [ticker, statement],
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
        "rating",
        "target_price",
        "strong_buy",
        "buy",
        "hold",
        "sell",
        "strong_sell",
    )
    _ANALYST_RATINGS_VALUE_COLS = (
        "rating",
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
    _TICKER_PROFILE_COLS = (
        "id",
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
    _ANALYST_FORECASTS_VALUE_COLS = tuple(
        c for c in _ANALYST_FORECASTS_COLS if c not in ("ticker", "period_end", "period_relative")
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

    def upsert_ticker_profile(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="tickers",
            cols=self._TICKER_PROFILE_COLS,
            pk=("id",),
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
        ``snapshot_col`` is strictly older than the latest stored row
        always pass through to INSERT, since they cannot collide on the
        ``(identity_cols, snapshot_col)`` PK.

        Returns the net number of rows added to ``table`` (count after −
        count before). Same-date corrections that hit the ON CONFLICT
        path return 0 since they update in place.

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
        any_value_changed = " OR ".join(f"latest.{c} IS DISTINCT FROM _in.{c}" for c in value_cols)
        update_clause = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in (*identity_cols, snapshot_col)
        )
        partition = ", ".join(identity_cols)
        col_list = ", ".join(cols)
        pk_list = ", ".join((*identity_cols, snapshot_col))

        self.con.register("_in", df[cols])
        try:
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

    # ---- escape hatch -------------------------------------------------------

    def sql(self, query: str, params: list | None = None) -> pd.DataFrame:
        cur = self.con.execute(query, params) if params else self.con.execute(query)
        return cur.fetchdf()
