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

MIGRATIONS_DIR = Path(__file__).parent / "migrations_duckdb"

_PRICE_COLS = ("ticker", "date", "open", "high", "low", "close", "adj_close", "volume")
_FUND_COLS = ("ticker", "period_end", "frequency", "statement", "line_item", "value")


class DuckDBLake:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con: duckdb.DuckDBPyConnection | None = duckdb.connect(str(self._path))

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
            row[0]
            for row in self.con.execute("SELECT version FROM schema_migrations").fetchall()
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
        return int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    # ---- prices -------------------------------------------------------------

    def upsert_prices(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        self.con.register("_in", df[list(_PRICE_COLS)])
        try:
            self.con.execute(
                """
                INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume)
                SELECT ticker, date, open, high, low, close, adj_close, volume FROM _in
                ON CONFLICT (ticker, date) DO UPDATE SET
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

    def get_prices(self, ticker: str, start: Any, end: Any) -> pd.DataFrame:
        return self.con.execute(
            """
            SELECT ticker, date, open, high, low, close, adj_close, volume
            FROM prices
            WHERE ticker = ? AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            [ticker, start, end],
        ).fetchdf()

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

    # ---- extended fundamentals (migration 002) ------------------------------

    _DIVIDEND_COLS = (
        "ticker", "ex_date", "amount", "currency",
        "pay_date", "record_date", "declaration_date",
    )
    _INSIDER_COLS = (
        "ticker", "date", "owner_name", "owner_relation", "transaction_code",
        "shares", "price", "value", "vendor_id",
    )
    _NEWS_COLS = ("ticker", "published_at", "title", "url", "source_name", "sentiment")
    _NEWS_SENTIMENT_COLS = ("ticker", "date", "sentiment", "article_count")
    _ANALYST_EST_COLS = ("ticker", "period_end", "metric", "value")
    _ANALYST_RATINGS_COLS = (
        "ticker", "rating", "target_price",
        "strong_buy", "buy", "hold", "sell", "strong_sell", "updated_at",
    )
    _SHARES_OUT_COLS = ("ticker", "date", "shares")
    _EMPLOYEES_COLS = ("ticker", "date", "count")
    _SEGMENTATION_COLS = ("ticker", "period_end", "dimension", "segment", "value")
    _TICKER_PROFILE_COLS = (
        "id", "exchange", "currency", "name", "country_iso", "ipo_date",
        "sector", "industry", "fiscal_year_end", "web_url",
        "is_delisted", "is_bank",
        "beta", "short_percent", "insider_ownership_percent",
        "institutional_ownership_percent", "employee_count", "esg_score",
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
        # Deduplicates on (ticker, date, owner_name, transaction_code, shares)
        # via the unique index; the synthetic id column is excluded from insert.
        if df.empty:
            return 0
        self.con.register("_in", df[list(self._INSIDER_COLS)])
        try:
            self.con.execute(
                f"""
                INSERT INTO insider_transactions ({", ".join(self._INSIDER_COLS)})
                SELECT {", ".join(self._INSIDER_COLS)} FROM _in
                ON CONFLICT (ticker, date, owner_name, transaction_code, shares)
                DO UPDATE SET
                    owner_relation = EXCLUDED.owner_relation,
                    price = EXCLUDED.price,
                    value = EXCLUDED.value,
                    vendor_id = EXCLUDED.vendor_id
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

    def upsert_analyst_estimates(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="analyst_estimates",
            cols=self._ANALYST_EST_COLS,
            pk=("ticker", "period_end", "metric"),
        )

    def upsert_analyst_ratings(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="analyst_ratings",
            cols=self._ANALYST_RATINGS_COLS,
            pk=("ticker",),
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

    def upsert_ticker_profile(self, df: pd.DataFrame) -> int:
        return self._upsert(
            df,
            table="tickers",
            cols=self._TICKER_PROFILE_COLS,
            pk=("id",),
        )

    # ---- generic upsert helper ---------------------------------------------

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

    # ---- escape hatch -------------------------------------------------------

    def sql(self, query: str, params: list | None = None) -> pd.DataFrame:
        cur = self.con.execute(query, params) if params else self.con.execute(query)
        return cur.fetchdf()
