# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

An end-to-end multi-asset research + trading system organized as four blocks: ingestion → strategy lab → strategy store → production tick. Inspired by a sibling `stock_analysis` repo but deliberately redesigned for scale, pluggability, and live-trading readiness.

Asset classes (closed set in `core.types.AssetClass`): `equity`, `crypto`, `commodity`, `bond`. Equities are the default and have the richest metadata surface (fundamentals, dividends, insiders, analysts, ESG, …). Non-equity classes use the same `bars` time series and add small per-class profile tables (`crypto_profiles`, `bond_profiles` + `bond_yield_history`, `commodity_contracts`).

Full architecture lives in `docs/architecture.md`; each block has its own sub-plan in `docs/blocks/`.

## Commands

All commands assume `uv` (installed via `pipx install uv`). Run from the repo root.

```bash
uv sync                         # install deps into .venv with Python 3.12+
uv run pytest -n auto           # full suite in parallel on every core (~1 min); no network
uv run pytest                   # same suite on one core; use for a single file or test
uv run pytest -m live           # live API contract tests (requires STONKS_RUN_LIVE_TESTS=1 and a real EODHD key in .env)
uv run pytest tests/unit/test_params_spec.py -k "tunable"   # single test by path + keyword
uv run ruff check .
uv run ruff format .
uv run stonks db init           # migrates both lake.duckdb and state.sqlite
uv run stonks db info           # lists tables + row counts in both stores
uv run stonks ingest prices --tickers AAPL.US --since 2025-05-01
uv run stonks registry list [--status active|shadow|retired]
uv run stonks registry show <id>
uv run stonks registry promote <id>
uv run stonks registry retire <id>
uv run stonks tick [--dry-run] [--as-of YYYY-MM-DD] [--tickers AAPL.US,MSFT.US]
uv run python -m stonks.api.openapi   # regenerate web/openapi.json after changing routes/models
uv run python -m stonks.api.docs      # regenerate docs/api/rest.md from web/openapi.json
uv run python -m stonks.mcp.docs      # regenerate docs/api/mcp-tools.{json,md} after changing MCP tools
```

## Working rules (enforced)

- **Principles.** `docs/principles.md` lists the research, validation, risk and engineering rules every change must respect. Its backlog is `docs/research/book-lessons.md`.
- **TDD.** Write a failing unit test first, then the implementation. Every new component ships with unit tests. Integration tests live under `tests/integration/`; any test that hits a real network goes under `tests/integration/live/` and is gated by `@pytest.mark.live` + `STONKS_RUN_LIVE_TESTS=1`.
- **No live-API calls in default test runs.** Default `pytest` must be hermetic. Use `FakeDataSource` (canned data) for pipeline tests.
- **Secrets never land in git.** `.env` is gitignored; `.env.example` is the only checked-in template.
- **Vendor-agnostic schemas.** Schemas, lake tables, and column names describe domain concepts, never vendor JSON shapes. EODHD is the first `DataSource`; other adapters must populate the same tables without renaming columns or inventing parallel schemas. Concretely:
  - Fields whose vendor vocabulary varies (e.g. `before_after_market`, `security_type`, analyst-forecast `period_relative`) use **normalized literal values**; adapters map vendor strings to the canonical literal at parse time.
  - Vendor-specific fields with no cross-vendor analogue (e.g. EODHD's `HomeCategory`, `LogoURL`) are **not added** — they don't earn a column.
  - **Domain identifiers** (CUSIP, CIK, ISIN, OpenFigi, LEI) live on `TickerProfile`; the EODHD ticker (`AAPL.US`) is one access key among many.
  - **Column names use domain terms**, not vendor JSON keys (e.g. `change_pct` not `change_p`, `total_shares_pct` not `totalShares`).
- **Third-party libraries.** Don't reinvent the wheel — prefer well-maintained external libraries (e.g. `vectorbt`, `FinanceToolkit`) over hand-rolled implementations of non-trivial trading/finance logic. But every non-trivial third-party library must be wrapped behind one of our seams (`Strategy`, `Tuner`, `DataSource`, `Broker`, `Objective`, `SurvivalTest`, or a new ABC if none fit) so vendor-specific types, naming, and quirks never leak into `core/` or downstream blocks. Trivial utility libraries (numpy, pandas, scipy) are exempt.

## Architecture in one screen

- **`core/`** — dependency-free primitives: `types.py` (Bar, Fundamentals, Order, Fill, Portfolio), `params.py` (`ParameterSpec`, `Params`, `ParamSpace`), `protocols.py` (`Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`). Everything downstream imports from here.
- **`ingest/`** — pluggable `DataSource` ABC + `IngestPipeline` that normalizes and idempotently upserts into the lake. EODHD is the first source. Adding Yahoo Finance is a one-file drop-in.
- **`store/`** — `DuckDBLake` (interval-aware `bars`, three financial-statement tables, metadata, later features/artifacts) and `SqliteState` (strategies, survival_reports, tick_runs, orders, fills, portfolio_snapshots). Migrations live in `store/migrations_duckdb/*.sql` and `store/migrations_sqlite/*.sql`, applied in order at startup. The `bars` table is keyed by `(ticker, timestamp, interval)` and accepts any `Interval` code (1m, 5m, 15m, 30m, 1h, 4h, 12h, 1d, 1w); the old `prices` API is preserved as a daily shim. `SqliteState` is a thin foundation — domain helpers (`register_strategy`, `place_order`) belong to the blocks that own each table.
- **`strategies/`** — `BaseStrategy` + examples (`buy_and_hold`, `momentum`). Rule-based, technical, aggregator, ML, hybrid all satisfy the `Strategy` Protocol. **Feature extraction lives inside each strategy**; there is intentionally no shared "features pipeline" stage. `features/library.py` is an optional toolkit of reusable helpers (`ttm`, `rolling_zscore`, `trailing_return`, …), nothing more. Strategies declare `applicable_asset_classes: tuple[AssetClass, ...]` (default `("equity",)`); the Ranker drops universe tickers outside that set.
- **`lab/`** — `lab.runner` orchestrates tune → fit → survival suite → verdict. `GridTuner` + `RandomTuner`, `SharpeObjective`/`CAGRObjective`/`FinalReturnObjective`, and four survival tests (`oos`, `period_stability`, `perturbation`, `drift`). `Tuner` and `SurvivalTest` are strategy-agnostic; a new strategy never touches them, and a new tuner/test never touches any strategy.
- **`registry/`** — `StrategyRegistry` over SQLite for metadata + `ArtifactBundle` on disk at `data/artifacts/<id>/` (`meta.json`, `params.json`, `reports/<test_id>.json`, optional `fitted_state.joblib`). Load round-trips a strategy via `importlib` from the stored class path.
- **`backtest/`** — interval-aware `Backtester` + `SimulatedBroker` (idempotent via `client_id`) + `BacktestReport` with Sharpe / max-drawdown / CAGR. `BacktestConfig.interval: Interval` (default `DAY_1`) drives which bar granularity the engine iterates; `rebalance_every_bars` is a bar count, not a day count, so the same config works for daily and sub-daily backtests. Orders decided on bar t fill at bar t+1's open (no same-bar look-ahead), equity is marked at each bar's close with the last close carried forward for tickers with no bar, and Sharpe is annualized per interval. Shares the `Broker` Protocol with the execution/production layers.
- **`execution/` + `production/`** — `make_client_id(as_of, strategy_id, ticker, side)` helper (keyed by date, not tick, so a rerun of a crashed tick reproduces the same ids); `Ranker.rank(as_of)` walks active strategies × universe; `run_tick(...)` is the one-shot entrypoint invoked via `stonks tick`. Stateless across invocations; all state lives in DuckDB + SQLite. Portfolio auto-seeds from `production.initial_cash` on first tick; thereafter it resumes from the latest `portfolio_snapshots` row.

## Canonical schemas (current)

**Lake (DuckDB):**
- `instruments (id, asset_class, exchange, currency, ipo_date, sector, industry, is_delisted, … + profile columns from migration 002)` — abstract instrument table, renamed from `tickers` in migration 007. `asset_class ∈ {equity, crypto, commodity, bond}` (default `equity`).
- `bars (ticker, timestamp, interval, open, high, low, close, adj_close, volume; PK (ticker, timestamp, interval))` — canonical OHLCV at any granularity, asset-class-agnostic.
- `prices` — read-only view over `bars` where `interval='1d'`, for back-compat SQL only. Write through `upsert_prices` (daily shim) or `upsert_bars` (any interval).
- Financial statements (equity-only, migration 008): one wide table per statement, all keyed by `(ticker, period_end, frequency)`:
  - `income_statement (ticker, period_end, frequency, filing_date, currency, revenue, cost_of_revenue, gross_profit, operating_income, net_income, ebitda, …)`
  - `balance_sheet (ticker, period_end, frequency, filing_date, currency, total_assets, current_assets, cash, total_liabilities, total_stockholder_equity, …)`
  - `cash_flow_statement (ticker, period_end, frequency, filing_date, currency, operating_cash_flow, investing_cash_flow, financing_cash_flow, capital_expenditures, free_cash_flow, …)`
  Vendors omit lines that don't apply to a given filer (banks have no `cost_of_revenue`, software firms no `inventory`); `upsert_<statement>` reindexes sparse DataFrames so missing columns land as NULL on first INSERT, and uses `COALESCE(EXCLUDED.col, table.col)` on UPDATE so a NULL in the input does **not** overwrite a prior non-NULL value (real values still overwrite — the typical "vendor restated revenue" path).
- Per-asset-class profiles (migration 007): `crypto_profiles`, `bond_profiles`, `bond_yield_history`, `commodity_contracts`. Equity-shaped tables (statements, dividends, …) simply hold no rows for non-equity instruments — no FK enforcement.
- `macro_indicators (country_iso, indicator, observation_date, period, country_name, value; PK (country_iso, indicator, observation_date))` — country-level macroeconomic time series (GDP, inflation, unemployment, …). `country_iso` is ISO 3166-1 alpha-3 (`USA`, `DEU`); `indicator` is canonical `lower_snake_case` (`real_gdp_total`, `inflation_consumer_prices_annual`). `period ∈ {annual, quarterly, monthly}` (NULL on unknown vendor cadence). The set of indicators is open (vendors keep adding new series), so the column stays free-text rather than a closed Literal — adapters normalize their vendor strings into snake_case at parse time.
- `ingest_runs (id, source, kind, started_at, finished_at, tickers_ok, tickers_failed, status, error)`
- Equity metadata surface (from migration 002): `dividends`, `insider_transactions`, `news`, `news_sentiment`, `analyst_estimates`, `analyst_ratings`, `shares_outstanding`, `employee_count`, `segmentation`.

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
- **`Literal` vs `Enum`.** Default to `Literal[...]` for closed sets of stringly-typed tags that flow through serialization boundaries (DB columns, JSON, vendor APIs). Reach for `StrEnum` (3.11+) when the set grows behavior (methods, predicates), needs iteration as a first-class operation, or when named symbols at call sites read better than bare strings. Don't stick with `Literal` just because neighboring code uses it.

## Known external limits

- EODHD free tier: prices only, ≤ 1-year window.
- Treat any ticker-level vendor failure as soft-fail (log + continue); the ingestion run row records `tickers_ok`/`tickers_failed`.
