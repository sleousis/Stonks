# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

An end-to-end equity research + trading system organized as four blocks: ingestion → strategy lab → strategy store → production tick. Inspired by a sibling `stock_analysis` repo but deliberately redesigned for scale, pluggability, and live-trading readiness.

Full architecture lives in `docs/architecture.md`; each block has its own sub-plan in `docs/blocks/`.

## Commands

All commands assume `uv` (installed via `pipx install uv`). Run from the repo root.

```bash
uv sync                         # install deps into .venv with Python 3.12+
uv run pytest                   # unit + integration tests; no network
uv run pytest -m live           # live API contract tests (requires STONKS_RUN_LIVE_TESTS=1 and a real EODHD key in .env)
uv run pytest tests/unit/test_params_spec.py -k "tunable"   # single test by path + keyword
uv run ruff check .
uv run ruff format .
uv run stonks db init           # creates data/lake.duckdb and runs migrations
uv run stonks db info           # lists tables + row counts
uv run stonks ingest prices --tickers AAPL.US --since 2025-05-01
```

## Working rules (enforced)

- **TDD.** Write a failing unit test first, then the implementation. Every new component ships with unit tests. Integration tests live under `tests/integration/`; any test that hits a real network goes under `tests/integration/live/` and is gated by `@pytest.mark.live` + `STONKS_RUN_LIVE_TESTS=1`.
- **No live-API calls in default test runs.** Default `pytest` must be hermetic. Use `FakeDataSource` (canned data) for pipeline tests.
- **Secrets never land in git.** `.env` is gitignored; `.env.example` is the only checked-in template.

## Architecture in one screen

- **`core/`** — dependency-free primitives: `types.py` (Bar, Fundamentals, Order, Fill, Portfolio), `params.py` (`ParameterSpec`, `Params`, `ParamSpace`), `protocols.py` (`Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`). Everything downstream imports from here.
- **`ingest/`** — pluggable `DataSource` ABC + `IngestPipeline` that normalizes and idempotently upserts into the lake. EODHD is the first source. Adding Yahoo Finance is a one-file drop-in.
- **`store/`** — `DuckDBLake` (prices, fundamentals, later features/artifacts) and `SqliteState` (strategies, survival_reports, tick_runs, orders, fills, portfolio_snapshots). Migrations live in `store/migrations_duckdb/*.sql` and `store/migrations_sqlite/*.sql`, applied in order at startup. `SqliteState` is a thin foundation — domain helpers (`register_strategy`, `place_order`) belong to the blocks that own each table.
- **`strategies/`** (post-MVP) — `BaseStrategy` + examples. Rule-based, technical, aggregator, ML, hybrid all satisfy the `Strategy` protocol. **Feature extraction lives inside each strategy**; there is intentionally no shared "features pipeline" stage. `features/library.py` is an optional toolkit of reusable helpers, nothing more.
- **`lab/`** (post-MVP) — `lab.runner` orchestrates tune → fit → backtest → survival suite → register. `Tuner` and `SurvivalTest` are strategy-agnostic; a new strategy never touches them, and a new tuner/test never touches any strategy.
- **`registry/`** (post-MVP) — `StrategyRegistry` over SQLite for metadata + on-disk artifact bundles at `data/artifacts/<id>/`.
- **`backtest/`** (post-MVP) — date-driven engine + `SimulatedBroker`. Shares the `Broker` protocol with the live execution layer, so strategies don't know or care which world they run in.
- **`execution/` + `production/`** (post-MVP) — `Broker` protocol + live impls; `stonks tick` is the one-shot entrypoint an external scheduler (cron / systemd timer) invokes. Stateless across invocations; all state lives in DuckDB + SQLite. Orders carry `client_id` for idempotent re-submission.

## Canonical schemas (current)

**Lake (DuckDB):**
- `tickers (id, exchange, currency, ipo_date, sector, industry, is_delisted)`
- `prices (ticker, date, open, high, low, close, adj_close, volume; PK (ticker, date))`
- `fundamentals (ticker, period_end, frequency, statement, line_item, value; PK (ticker, period_end, frequency, statement, line_item))`
- `ingest_runs (id, source, kind, started_at, finished_at, tickers_ok, tickers_failed, status, error)`

**State (SQLite):**
- `strategies (id PK, class_path, params_json, artifact_path, status, created_at, updated_at)` — `status ∈ {active, shadow, retired}`
- `survival_reports (id, strategy_id FK, test_id, passed, metrics_json, notes, created_at)`
- `tick_runs (id PK /* ulid */, started_at, finished_at, status, summary_json)`
- `orders (client_id PK /* idempotency */, tick_id FK, strategy_id FK, ticker, side, quantity, order_type, limit_price, status, broker_order_id, created_at, updated_at)`
- `fills (id, order_client_id FK, ticker, quantity, price, fee, filled_at)`
- `portfolio_snapshots (id, tick_id FK, taken_at, cash, positions_json, total_value)`

## Conventions to match

- Settings are `pydantic` dataclasses / `BaseSettings`; never long kwarg lists.
- Abstract base classes + Protocols are the seam for plugging in new behavior: `DataSource`, `Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`.
- Logging via `stonks.logging.get_logger(name)` (structlog JSON). Every cross-block action carries a `run_id` / `tick_id` so logs correlate.
- Free-tier EODHD only returns EOD prices; the fundamentals endpoint returns a text error. Live fundamentals tests must handle that signal gracefully (skip, not fail).

## Known external limits

- EODHD free tier: prices only, ≤ 1-year window.
- Treat any ticker-level vendor failure as soft-fail (log + continue); the ingestion run row records `tickers_ok`/`tickers_failed`.
