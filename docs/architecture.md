# Stonks — Architecture Overview

End-to-end equity research + trading system. Four blocks: Ingestion → Strategy Lab → Strategy Store → Production Tick. Everything behind the store is strategy-agnostic; the strategy owns its own feature extraction and parameter descriptions.

## Fixed design decisions

| Axis | Decision |
|---|---|
| Execution target | Live trading eventually; broker seam abstract from day one; orders idempotent via `client_id`. |
| Universe size | 5k+ tickers. |
| Analytical store | DuckDB (single file `data/lake.duckdb`). |
| Transactional state | SQLite (single file `data/state.sqlite`). |
| Scheduling | External (cron / systemd timer). Each production tick is a fresh Python process. |
| Strategy shape | Protocol. Rule-based, technical, aggregator, ML, hybrid all satisfy it. `fit()` is optional. |
| Feature extraction | Lives *inside* each strategy. No shared feature-pipeline layer. |
| Parameter handling | Strategies declare `ParameterSpec` list; tuner reads only that. |
| Survival tests | Composable protocol; suite runs all four (OOS, period stability, perturbation, drift). |
| Language / tooling | Python 3.12+, uv, ruff, pytest, structlog, pydantic-settings, typer. |

## Top-level diagram

```mermaid
flowchart LR
  subgraph EXT[External world]
    EODHD[EODHD API]
    YAHOO[Yahoo Finance API]
    BRK[Live Broker - future]
    CRON[cron / systemd timer]
  end

  subgraph ING[Block 1: Ingestion]
    DS[DataSource]
    PIPE[IngestPipeline]
  end

  subgraph STORE[Storage]
    LAKE[(DuckDBLake<br/>prices, fundamentals,<br/>features, backtest artifacts)]
    STATE[(SqliteState<br/>portfolio, orders,<br/>registry, runs)]
  end

  subgraph LAB[Block 2: Strategy Lab]
    TUNER[Tuner]
    BT[Backtester]
    SUITE[SurvivalSuite]
    VERDICT{Profitable<br/>and robust?}
  end

  subgraph REG[Block 3: Strategy Store]
    REGISTRY[(StrategyRegistry)]
  end

  subgraph PROD[Block 4: Production Tick]
    RANK[Ranker]
    EXEC[Executor / Broker]
    TICK[ProductionTick]
  end

  EODHD --> DS
  YAHOO --> DS
  DS --> PIPE
  PIPE -->|upsert| LAKE

  LAKE --> TUNER
  LAKE --> BT
  LAKE --> SUITE
  TUNER --> BT
  BT --> SUITE
  SUITE --> VERDICT
  VERDICT -- yes --> REGISTRY
  VERDICT -- no --> LAB

  CRON --> TICK
  TICK --> RANK
  REGISTRY --> RANK
  LAKE --> RANK
  STATE --> RANK
  RANK --> EXEC
  EXEC --> BRK
  EXEC --> STATE
```

## Interface seams

```mermaid
classDiagram
    class Strategy {
      <<Protocol>>
      +id: str
      +parameter_spec() ParamSpace$
      +__init__(params)
      +extract_features(ticker, as_of, lake) Features
      +fit(dataset) None
      +estimate_return(ticker, as_of, lake) float?
      +decide(ranked, portfolio) list~Order~
      +save(path) None
      +load(path) Strategy$
    }
    class ParameterSpec {
      +name: str
      +kind: float|int|categorical|bool
      +default: Any
      +bounds: tuple|list|None
      +tunable: bool
      +description: str
    }
    class Tuner {
      <<Protocol>>
      +tune(strategy_cls, param_space, objective, dataset, budget) TunerResult
    }
    class Objective {
      <<Protocol>>
      +name: str
      +direction
      +score(strategy, dataset) float
    }
    class SurvivalTest {
      <<Protocol>>
      +id: str
      +run(strategy, context) SurvivalReport
    }
    class Broker {
      <<Protocol>>
      +fetch_portfolio()
      +place_order(order) Fill?
      +reconcile()
    }
    Strategy --> ParameterSpec : declares
    Tuner --> Strategy : factory
    Tuner --> Objective : scores with
    SurvivalTest --> Strategy
    Broker <|.. SimulatedBroker
    Broker <|.. LiveBrokerImpl
```

## Repository layout

```
Stonks/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── .env.example
├── .gitignore
├── config/default.toml
├── data/                       # gitignored
├── scripts/                    # operational runners (post-MVP)
├── docs/
│   ├── architecture.md
│   └── blocks/
│       ├── 01_ingestion.md
│       ├── 02_storage.md
│       ├── 03_strategy_lab.md
│       ├── 04_strategy_store.md
│       └── 05_production_tick.md
├── src/stonks/
│   ├── config.py  logging.py  cli.py
│   ├── core/       params.py    # + types.py protocols.py (post-MVP)
│   ├── ingest/     sources/{base,eodhd,yahoo}.py  name_mapper.py  schemas.py  pipeline.py
│   ├── store/      lake.py      # + state.py (post-MVP)
│   ├── features/   library.py                     (post-MVP)
│   ├── strategies/                                (post-MVP)
│   ├── lab/                                       (post-MVP)
│   ├── registry/                                  (post-MVP)
│   ├── backtest/                                  (post-MVP)
│   ├── execution/                                 (post-MVP)
│   └── production/                                (post-MVP)
└── tests/          unit/  integration/  fixtures/
```

## Cross-cutting concerns

- **Config.** `pydantic-settings` layered over env + `config/default.toml`. Singleton via `load_settings()`. Secrets never in TOML; only in `.env` / env vars.
- **Logging.** `structlog` JSON. Every block emits structured events with `run_id`, `block`, `strategy_id` (where applicable) so logs are correlatable across a production tick.
- **Error handling philosophy.** Per-ticker soft-fail during ingestion and ranking (log + continue). Hard-fail on store corruption or broker reconcile mismatch.
- **Testing.** No live API calls in default `pytest`. `STONKS_RUN_LIVE_TESTS=1` + a real API key enables a small contract-test suite.
- **Idempotency.** Ingestion upserts are idempotent (PK + `ON CONFLICT`). Orders carry a `client_id`. Production ticks are safe to re-run for the same date.

## Bar store (Parquet or DuckDB table)

DuckDB lets one process at a time open a database file for writing, so while `stonks serve` holds `lake.duckdb`, the CLI, the MCP server and lab worker processes cannot read the `bars` table. The lake therefore reads and writes bars through a `BarStore` seam (`store/bars.py`) with two implementations:

- `DuckDBTableBarStore`: the `bars` table in the lake file. It is the default and what every test lake uses.
- `ParquetBarStore`: hive-partitioned files at `<lake dir>/bars/interval=<code>/ticker=<id>/year=<yyyy>/part-0.parquet`. Any number of processes can read them while one process writes. Path values are percent-encoded, so `ES=F` is stored as `ticker=ES%3DF`.

The public `DuckDBLake` API is unchanged. `upsert_bars`, `upsert_prices`, `aggregate_bars` and `get_bars` go through the store. On a Parquet lake the connection gets a temporary `bars` view over `read_parquet(..., hive_partitioning = true)`, so existing SQL (the `prices` view, the engine, `lake.sql`) runs as before.

How Parquet writes stay safe:

- An upsert rewrites each partition it touches. It reads the existing file, merges in the new rows (the last write wins per timestamp), writes a `*.tmp` file in the same directory and `os.replace`s it over `part-0.parquet`. Readers therefore see the old file or the new one, never a half-written file. Stray part files are merged in and removed (compaction).
- Each `(interval, ticker)` series has an OS lock file under `bars/_locks/`, which the operating system releases if the writer crashes. Two writers never interleave on a partition, and readers never take a lock.
- On Windows `os.replace` fails while a reader has the file open, so the writer retries (for up to 60 s). Readers never wait.
- A multi-year upsert is atomic per partition, not across partitions. Timestamps are stored as naive UTC, the same as in the table.

The lake records its store in `lake_settings` (migration 012), so every process that opens the file agrees on it without extra configuration. Future migrations that alter `bars` must handle both stores.

Switching: set `[lake.bars] backend = "parquet"` in the config, stop `stonks serve`, and run `uv run python -m stonks.store.bars_migrate`, or call `DuckDBLake.migrate_bars_to_parquet()`. The command copies every series, checks the row count and checksum of each `(ticker, interval)`, and only then drops the table and switches. On a mismatch nothing changes. `--to duckdb` (`migrate_bars_to_duckdb()`) switches back and leaves the Parquet files for you to delete. A new lake opened with `DuckDBLake(path, bar_backend="parquet")` starts in Parquet.

Lab snapshots (`lab/parallel.py`) for a Parquet lake export every table except the bars. The universe's partitions are hard-linked next to the snapshot (copied if the snapshot is on another volume) and cut at the dataset's end date, so workers read the files directly. Because writers replace files and never edit them, the run's data cannot change underneath it. Results are bit-identical to the table store.

## Per-block documents

- [Block 1 — Ingestion](blocks/01_ingestion.md) (implemented in MVP)
- [Block 2 — Storage](blocks/02_storage.md) (lake half implemented in MVP)
- [Block 3 — Strategy Lab](blocks/03_strategy_lab.md)
- [Block 4 — Strategy Store](blocks/04_strategy_store.md)
- [Block 5 — Production Tick](blocks/05_production_tick.md)
