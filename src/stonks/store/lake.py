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

    # ---- escape hatch -------------------------------------------------------

    def sql(self, query: str, params: list | None = None) -> pd.DataFrame:
        cur = self.con.execute(query, params) if params else self.con.execute(query)
        return cur.fetchdf()
