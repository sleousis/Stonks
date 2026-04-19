# Block 2 — Storage (DuckDB lake + SQLite state)

## Purpose

Two physically-separate stores behind clean interfaces. Swap-for-Postgres is a one-class change.

- **`DuckDBLake`** — append-heavy, analytical, columnar. Prices, fundamentals, features, backtest artifacts.
- **`SqliteState`** (post-MVP) — transactional, small-volume. Portfolio, orders, fills, strategy registry, run ledgers.

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

## Lake schema (MVP, `migrations/001_init.sql`)

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

Feature tables, backtest artifacts, and survival-report blobs are added via future migrations.

## `SqliteState` (planned — post-MVP)

```sql
CREATE TABLE strategies (
    id               TEXT PRIMARY KEY,
    class_path       TEXT NOT NULL,
    params_json      TEXT NOT NULL,
    artifact_path    TEXT,
    status           TEXT NOT NULL,     -- 'active' | 'shadow' | 'retired'
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE survival_reports (
    id               INTEGER PRIMARY KEY,
    strategy_id      TEXT NOT NULL REFERENCES strategies(id),
    test_id          TEXT NOT NULL,
    passed           INTEGER NOT NULL,
    metrics_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE portfolio_snapshots ( ... );
CREATE TABLE orders  ( id TEXT PRIMARY KEY /* client_id */, ... );
CREATE TABLE fills   ( ... );
CREATE TABLE tick_runs ( ... );
```

## Migrations

Plain SQL files in `src/stonks/store/migrations/`, applied in lexical order at `migrate()` time. `schema_migrations (version INT PK, applied_at TIMESTAMP)` tracks applied versions. Never edit an applied migration; always add a new one.

## Why not Postgres (yet)

- No server to run or back up.
- Single-machine quant workstation is the primary target.
- Postgres becomes necessary only when (a) multiple lab workers need to write artifacts simultaneously, or (b) the tick runs on a different host than the lake. Swap is a `DuckDBLake` → `PostgresLake` class change; upsert SQL is near-identical.

## Testing

- `tests/integration/test_lake_roundtrip.py` — tmp DB; migrate; upsert (idempotent, update-on-conflict); read; run ledger lifecycle; empty-DF is no-op.
