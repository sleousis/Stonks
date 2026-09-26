"""SQLite-backed transactional state store.

This is a **thin foundation**: connection management, SQL migrations, table
introspection, and a generic query surface. Higher-level domain methods
(e.g. ``register_strategy``, ``place_order``) live with the blocks that own
those tables (registry, execution, production), not here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations_sqlite"

#: How long a connection waits for another one's write lock before
#: ``database is locked``. The API, the scheduler and the tick all write.
BUSY_TIMEOUT_SECONDS = 10.0


class SqliteState:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con: sqlite3.Connection | None = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit; explicit BEGIN when we want a tx
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=BUSY_TIMEOUT_SECONDS,
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")

    def __enter__(self) -> SqliteState:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            raise RuntimeError("SqliteState connection is closed")
        return self._con

    # ---- migrations --------------------------------------------------------

    def migrate(self) -> None:
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in self.con.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.stem.split("_", 1)[0])
            if version in applied:
                continue
            # executescript() COMMITs any pending transaction before running,
            # so an outer BEGIN from Python wouldn't cover it. Instead the
            # BEGIN/COMMIT and the schema_migrations row go *inside* the
            # script: the migration and its version row land atomically, and
            # a failure part-way leaves neither (we ROLLBACK the open tx).
            # version is an int and the timestamp is ISO-8601, so inlining
            # them is safe.
            applied_at = datetime.now(UTC).isoformat(timespec="seconds")
            script = (
                "BEGIN;\n"
                f"{path.read_text(encoding='utf-8')}\n;\n"
                f"INSERT INTO schema_migrations VALUES ({version}, '{applied_at}');\n"
                "COMMIT;\n"
            )
            try:
                self.con.executescript(script)
            except Exception:
                if self.con.in_transaction:
                    self.con.execute("ROLLBACK")
                raise

    def applied_migrations(self) -> list[int]:
        if not self._has_table("schema_migrations"):
            return []
        return [
            row[0]
            for row in self.con.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]

    def _has_table(self, name: str) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [name]
        ).fetchone()
        return row is not None

    # ---- introspection + generic query surface -----------------------------

    def tables(self) -> list[str]:
        rows = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    def count_rows(self, table: str) -> int:
        # Whitelist the name against sqlite_master so we can interpolate into
        # the SQL string safely even for CLI/config-sourced table names.
        known = set(self.tables())
        if table not in known:
            raise ValueError(f"unknown table {table!r}")
        row = self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def execute(self, query: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        return self.con.execute(query, params or [])

    def sql(self, query: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
        return self.execute(query, params).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes into one atomic unit: COMMIT on clean exit,
        ROLLBACK + re-raise on exception.

        Re-entrant: when a transaction is already open (an outer
        ``transaction()`` or an explicit BEGIN), the inner call joins it
        and the outermost owner keeps the COMMIT/ROLLBACK responsibility —
        an exception escaping the inner block rolls back everything once
        it reaches the outer one. Mirrors ``DuckDBLake.transaction()``.

        ``BEGIN IMMEDIATE`` takes the write lock up front. A deferred
        transaction that reads and then writes cannot upgrade its lock
        once another connection has committed (WAL): SQLite then fails at
        once with ``database is locked`` instead of waiting. Taking the lock
        first makes a concurrent writer wait (``busy_timeout``) instead.
        Any exit by exception, ``KeyboardInterrupt`` included, rolls back.
        """
        if self.con.in_transaction:
            yield
            return
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")
