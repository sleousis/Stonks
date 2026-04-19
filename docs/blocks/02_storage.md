# Block 2 — Storage (DuckDB lake + SQLite state)

> Status: **both halves implemented**. Lake in the initial commit, state foundation in the Block 2 follow-up.

## Purpose

Two physically-separate stores behind clean interfaces. Swap-for-Postgres is a one-class change.

- **`DuckDBLake`** — append-heavy, analytical, columnar. Prices, fundamentals, features, backtest artifacts.
- **`SqliteState`** — transactional, small-volume. Portfolio snapshots, orders, fills, strategy registry, survival reports, tick-run ledger.

## Why split

- Lake is read by parallel backtest + tuning jobs; DuckDB excels at large columnar scans with zero server.
- State is tiny but needs cross-process safety (cron tick, lab runner, manual CLI). SQLite's WAL mode handles this.
- Neither needs a server. Ops surface stays ~zero.

## `DuckDBLake` (implemented)

```python
class DuckDBLake:
    def __init__(self, path: Path): ...
    def migrate(self) -> None: ...
    def applied_migrations(self) -> list[int]: ...
    def tables(self) -> list[str]: ...
    def count_rows(self, table: str) -> int: ...

    def upsert_prices(self, df: DataFrame) -> int: ...
    def get_prices(self, ticker: str, start, end) -> DataFrame: ...
    def upsert_fundamentals(self, df: DataFrame) -> int: ...
    def get_fundamentals(self, ticker: str, statement: str | None = None) -> DataFrame: ...

    def open_ingest_run(self, source: str, kind: str) -> int: ...
    def close_ingest_run(self, run_id, tickers_ok, tickers_failed, status, error=None): ...

    def sql(self, query: str, params: list | None = None) -> DataFrame: ...  # escape hatch
```

## Lake schema

**Migrations live in `migrations_duckdb/`** and are applied in order:

- `001_init.sql` — `tickers`, `prices`, `fundamentals` (3 statements), `ingest_runs`.
- `002_extended_fundamentals.sql` — widens `tickers` with profile columns (beta, short_percent, employee_count, …) and adds the full non-statement metadata surface: `dividends`, `insider_transactions`, `news`, `news_sentiment`, `analyst_estimates`, `analyst_ratings`, `shares_outstanding`, `employee_count`, `segmentation` (one table covering both revenue and geographic dimensions).
- `003_intraday_bars.sql` — supersedes the daily-only `prices` table with an interval-aware `bars` table keyed by `(ticker, timestamp, interval)`. The `interval` column stores the canonical `Interval` code (`1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `12h`, `1d`, `1w`). A read-only `prices` view over the `interval='1d'` slice is recreated for backward-compat SQL; `upsert_prices` / `get_prices` are thin shims over `upsert_bars` / `get_bars` with `interval=Interval.DAY_1`. Same `bars` table holds mixed granularities for the same ticker (e.g. 5m live alongside 1d historical).

### `001_init.sql`

```sql
CREATE TABLE tickers (
    id            VARCHAR PRIMARY KEY,
    exchange      VARCHAR,
    currency      VARCHAR,
    ipo_date      DATE,
    sector        VARCHAR,
    industry      VARCHAR,
    is_delisted   BOOLEAN DEFAULT FALSE
);

CREATE TABLE prices (
    ticker     VARCHAR NOT NULL,
    date       DATE    NOT NULL,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    adj_close  DOUBLE,
    volume     BIGINT,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE fundamentals (
    ticker      VARCHAR NOT NULL,
    period_end  DATE    NOT NULL,
    frequency   VARCHAR NOT NULL,   -- 'Q' | 'A'
    statement   VARCHAR NOT NULL,   -- 'income' | 'balance' | 'cashflow'
    line_item   VARCHAR NOT NULL,
    value       DOUBLE,
    PRIMARY KEY (ticker, period_end, frequency, statement, line_item)
);

CREATE SEQUENCE ingest_runs_id_seq;
CREATE TABLE ingest_runs (
    id              INTEGER PRIMARY KEY DEFAULT nextval('ingest_runs_id_seq'),
    source          VARCHAR NOT NULL,
    kind            VARCHAR NOT NULL,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    tickers_ok      INTEGER DEFAULT 0,
    tickers_failed  INTEGER DEFAULT 0,
    status          VARCHAR,            -- 'running' | 'ok' | 'partial' | 'error'
    error           VARCHAR
);
```

### `002_extended_fundamentals.sql`

Profile columns added to `tickers`: `name`, `country_iso`, `fiscal_year_end`, `web_url`, `is_bank`, `beta`, `short_percent`, `insider_ownership_percent`, `institutional_ownership_percent`, `employee_count`, `esg_score`.

New tables:

| Table | Natural key | Purpose |
|---|---|---|
| `dividends` | `(ticker, ex_date)` | Historical cash dividends. |
| `insider_transactions` | `(ticker, date, owner_name, transaction_code, shares)` (unique index) | Insider trade event log. |
| `news` | `(ticker, published_at, title)` | News articles with optional per-article sentiment. |
| `news_sentiment` | `(ticker, date)` | Daily aggregate sentiment. |
| `analyst_estimates` | `(ticker, period_end, metric)` | Long-format analyst metrics (epsActual, epsEstimate, …). |
| `analyst_ratings` | `(ticker)` | Current consensus snapshot; upserted. |
| `shares_outstanding` | `(ticker, date)` | Historical share count. |
| `employee_count` | `(ticker, date)` | Historical headcount. |
| `segmentation` | `(ticker, period_end, dimension, segment)` | Revenue and geographic segmentations in one table, distinguished by `dimension`. |

Each lake method (`upsert_dividends`, `upsert_news`, `upsert_segmentation`, …) is idempotent via `INSERT … ON CONFLICT DO UPDATE`. Pipeline ingests the full surface via a single `MetadataBundle` returned by `DataSource.fetch_metadata`.

Feature tables and backtest-artifact tables are added via future migrations.

## `SqliteState` (implemented)

Deliberately **thin**: connection management, migrations, introspection, and a generic SQL surface. Domain helpers (`register_strategy`, `place_order`, etc.) are owned by the blocks that own each table — registry (Block 4) and execution + production (Block 5) — not baked into the store.

```python
class SqliteState:
    def __init__(self, path: Path): ...
    def migrate(self) -> None: ...
    def applied_migrations(self) -> list[int]: ...
    def tables(self) -> list[str]: ...
    def count_rows(self, table: str) -> int: ...
    def execute(self, query, params=None) -> sqlite3.Cursor: ...
    def sql(self, query, params=None) -> list[sqlite3.Row]: ...
    @contextmanager
    def transaction(self): ...
```

- Opens with `PRAGMA journal_mode=WAL` (multi-process readers OK) + `PRAGMA foreign_keys=ON`.
- `transaction()` is an explicit `BEGIN`/`COMMIT`/`ROLLBACK` context manager. Migrations use `executescript` (which auto-commits per statement) with `IF NOT EXISTS` DDL for idempotent re-runs.

## State schema (`migrations_sqlite/001_init.sql`)

```sql
CREATE TABLE strategies (
    id            TEXT PRIMARY KEY,
    class_path    TEXT NOT NULL,                            -- "pkg.mod:ClassName"
    params_json   TEXT NOT NULL,
    artifact_path TEXT,                                     -- NULL for rule-based
    status        TEXT NOT NULL
                  CHECK (status IN ('active', 'shadow', 'retired')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE survival_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id  TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    test_id      TEXT NOT NULL,
    passed       INTEGER NOT NULL CHECK (passed IN (0, 1)),
    metrics_json TEXT NOT NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE tick_runs (
    id           TEXT PRIMARY KEY,                          -- ulid
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL
                 CHECK (status IN ('running', 'ok', 'partial', 'error')),
    summary_json TEXT
);

CREATE TABLE orders (
    client_id       TEXT PRIMARY KEY,                      -- idempotency key
    tick_id         TEXT REFERENCES tick_runs(id),
    strategy_id     TEXT REFERENCES strategies(id),
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        REAL NOT NULL,
    order_type      TEXT NOT NULL
                    CHECK (order_type IN ('market', 'limit', 'stop', 'stop_limit')),
    limit_price     REAL,
    status          TEXT NOT NULL
                    CHECK (status IN ('pending', 'filled', 'partially_filled',
                                      'rejected', 'cancelled')),
    broker_order_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_client_id TEXT NOT NULL REFERENCES orders(client_id),
    ticker          TEXT NOT NULL,
    quantity        REAL NOT NULL,
    price           REAL NOT NULL,
    fee             REAL NOT NULL DEFAULT 0,
    filled_at       TEXT NOT NULL
);

CREATE TABLE portfolio_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id        TEXT REFERENCES tick_runs(id),
    taken_at       TEXT NOT NULL,
    cash           REAL NOT NULL,
    positions_json TEXT NOT NULL,
    total_value    REAL NOT NULL
);
```

Indexes on `(tick_id)`, `(strategy_id)`, and `(order_client_id)` cover the hot lookup paths (ranker, reconciler, registry browser).

## Migrations

Plain SQL files in:
- `src/stonks/store/migrations_duckdb/` — applied by `DuckDBLake.migrate()`.
- `src/stonks/store/migrations_sqlite/` — applied by `SqliteState.migrate()`.

Applied in lexical order. Each store has its own `schema_migrations (version, applied_at)` table. Never edit an applied migration; always add a new one. SQLite migrations must be statement-level idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`) because `executescript()` auto-commits per statement; DuckDB migrations get true transactional atomicity via `BEGIN`/`COMMIT`.

## Why not Postgres (yet)

- No server to run or back up.
- Single-machine quant workstation is the primary target.
- Postgres becomes necessary only when (a) multiple lab workers need to write artifacts simultaneously, or (b) the tick runs on a different host than the lake. Swap is a `DuckDBLake` → `PostgresLake` class change; upsert SQL is near-identical.

## Testing

- `tests/integration/test_lake_roundtrip.py` — tmp DB; migrate; upsert (idempotent, update-on-conflict); read; run ledger lifecycle; empty-DF is no-op.
- `tests/integration/test_state_roundtrip.py` — tmp DB; migrate (idempotent); `schema_migrations` versioning; FK enforcement; `CHECK` constraint rejection on invalid `status`/`side`; PK collision; `sql()` mapping row access; `transaction()` commit/rollback semantics; `close()` is idempotent.
