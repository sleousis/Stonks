# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

An end-to-end multi-asset research and trading system: ingest data into a lake, test strategies in a lab, register the survivors, and run a daily production tick that trades one or more portfolios. The CLI, REST API, MCP server and Angular web console all sit on one service layer.

Asset classes (closed set in `core.types.AssetClass`): `equity`, `crypto`, `commodity`, `bond`. Equities are the default and have the richest metadata (statements, dividends, insiders, analysts, ...). Other classes use the same `bars` time series plus small profile tables.

Read first: `docs/architecture.md` (the system), `docs/principles.md` (the research and risk rules every change must respect), `docs/operations.md`, `docs/deploy.md`, `docs/roadmap.md`, `docs/design/accounts-and-modes.md`, `docs/strategies/README.md`.

## Commands

All commands assume `uv` (`pipx install uv`). Run from the repo root.

```bash
uv sync                          # install deps into .venv (Python 3.12+)
uv run pytest -n auto            # unit + integration tests in parallel; no network (sockets reach loopback only). Takes several minutes (about 12 on 3 workers)
uv run pytest tests/unit/test_params_spec.py -k "tunable"   # one test by path + keyword
STONKS_RUN_LIVE_TESTS=1 uv run pytest -m live   # live API contract tests (real keys in .env)
uv run ruff check . && uv run ruff format .
uv run python -m tools.pyright_gate            # pyright, fails on errors not in the baseline
uv run pytest -n auto --cov --cov-report=json && uv run python -m tools.coverage_gate coverage.json
uv run --with cosmic-ray python -m tools.mutation --only pnl   # mutation testing (slow)

# Stores
uv run stonks db init            # migrate lake.duckdb and state.sqlite
uv run stonks db info            # tables and row counts
uv run python -m stonks.store.bars_migrate --to parquet   # move bars to the Parquet store

# Ingest (--source eodhd|yahoo where it applies)
uv run stonks ingest prices --tickers AAPL.US --since 2025-05-01
uv run stonks ingest fundamentals|metadata|intraday --tickers AAPL.US
uv run stonks ingest macro | tvl | exchanges
uv run stonks ingest aggregate --tickers AAPL.US --from 1h --to 4h
uv run stonks ingest all-intervals --tickers AAPL.US
uv run stonks audit statements [--tickers AAPL.US]   # statement audit (also runs after ingest fundamentals)

# Universes and on-demand data (docs/universes.md)
uv run stonks universe list | show ID | members ID --as-of YYYY-MM-DD
uv run stonks universe create ID [--kind list|exchange|rule|index] [--tickers ...|--csv FILE|--spec JSON]
uv run stonks universe refresh ID | delete ID --yes | import-index INDEX FILE
uv run stonks universe ensure ID --start ... --end ... [--interval 1d --source eodhd]

# Lab and registry
uv run stonks lab run momentum --start 2023-01-01 --end 2025-01-01 --tickers AAPL.US,MSFT.US --preset promotion
uv run stonks lab run ... --strict | --no-preflight   # data preflight: warnings as errors, or skip it
uv run stonks lab run ... --universe-id ID [--ensure-data]   # a stored universe, fetching missing bars first
uv run stonks lab sweep --start 2023-01-01 --end 2025-01-01
uv run stonks lab ic --strategy momentum --tickers AAPL.US [--events]   # signal IC and event study
uv run stonks lab run ... --objective cv_sharpe   # score trials on purged folds (also cv_cagr, cv_final_return)
uv run stonks registry list [--status active|shadow|retired]
uv run stonks registry show|history <id>
uv run stonks registry promote <id> [--override --reason "..."]
uv run stonks registry shadow|retire <id> --reason "..."
uv run stonks golive check <id>

# Production
uv run stonks tick [--dry-run] [--as-of YYYY-MM-DD] [--tickers AAPL.US,MSFT.US]
uv run stonks health [--notify]   # also opens or clears the global operational halt
uv run stonks halts list [--all]
uv run stonks halts kill --scope global|user|portfolio [--portfolio ID] [--flatten] --reason "..."
uv run stonks halts resume ID --reason "..."   # asks you to type RESUME TRADING
uv run stonks halts clear ID --reason "..."    # circuit-breaker or operational halt
uv run stonks pnl [--since YYYY-MM-DD] [--strategy <shadow-id>]
uv run stonks report [--backtest <job-or-strategy> --start ... --end ...]
uv run stonks tca summary|journal|order|note|edit-note|refresh   # transaction costs and the trade journal

# Servers
uv run stonks serve              # REST API + built console on 127.0.0.1:8000
uv run stonks mcp                # MCP server over the running API

# Operations
uv run stonks schedule run|next|runs|run-now JOB|check|metrics
uv run stonks backup backup|verify|restore|list|prune

# Operator entry points
uv run python -m stonks.notify vapid-keygen|test --user EMAIL|deliver
uv run python -m stonks.connections providers|list|connect|sync [--due]|...
uv run python -m stonks.security keygen
```

## Working rules (enforced)

- **TDD.** Write a failing unit test first, then the implementation. Every new component ships with unit tests. Integration tests live under `tests/integration/`; anything that hits a real network goes under `tests/integration/live/`, marked `@pytest.mark.live` and gated by `STONKS_RUN_LIVE_TESTS=1`.
- **Hermetic default runs.** Default `pytest` makes no network calls. Use `FakeDataSource` (canned data) for pipeline tests and the fake broker connection provider for connection tests.
- **Secrets never land in git.** `.env` is gitignored; `.env.example` is the only checked-in template. Secrets come from the environment (API token, broker keys, VAPID, SMTP, `STONKS_SECRET_KEYS`), never TOML.
- **Principles.** Research, validation, sizing and operations changes follow `docs/principles.md` (P-numbers are cited in code and tests).
- **Vendor-agnostic schemas.** Schemas, lake tables and column names describe domain concepts, never vendor JSON shapes. Every `DataSource` fills the same tables with the same columns:
  - Fields whose vendor vocabulary varies (`before_after_market`, `security_type`, analyst `period_relative`) use **normalized literal values**; adapters map vendor strings at parse time.
  - Vendor-only fields with no cross-vendor analogue (EODHD `HomeCategory`, `LogoURL`) are **not added**.
  - **Domain identifiers** (CUSIP, CIK, ISIN, OpenFigi, LEI) live on `TickerProfile`; the EODHD ticker (`AAPL.US`) is one access key among many.
  - **Column names use domain terms** (`change_pct` not `change_p`, `total_shares_pct` not `totalShares`).
- **Third-party libraries.** Prefer well-maintained libraries over hand-rolled trading or finance logic, but wrap every non-trivial one behind a seam (`DataSource`, `Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`, `BrokerConnection`, `PortfolioConstructor`, `RiskRule`, `BarStore`, a notification `Channel`, or a new ABC) so vendor types never leak into `core/` or other blocks. Examples: `yfinance` in `ingest/sources/yahoo.py`, `alpaca-py` in `execution/brokers/alpaca.py`, `exchange_calendars` in `scheduling/calendar.py`, `pywebpush` in `notify/webpush.py`, `cryptography` in `security/crypto.py`. numpy, pandas and scipy are exempt.
- **Registries, not lists.** Survival tests, portfolio constructors, risk rules, tick hooks and strategies are discovered from their packages. A new one is one new module; no central list is edited.

## Architecture in one screen

- **`core/`**: dependency-free primitives. `types.py` (Bar, Order, Fill, Portfolio, AssetClass), `interval.py`, `params.py` (`ParameterSpec`, `Params`, `ParamSpace`), `protocols.py` (`Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`), `corporate_actions.py`, `timeutil.py`.
- **`ingest/`**: `DataSource` ABC and `IngestPipeline` (normalize, validate, idempotent upsert, one `ingest_runs` row per run). Sources in `ingest/sources/`: `eodhd`, `yahoo` (wraps `yfinance`), `defillama` (DeFi TVL), picked by `--source` through `sources/registry.py`. `quality.py` checks every bar batch and moves bad rows to `quarantined_bars`; the pipeline can retry a failed ticker on a fallback source.
- **`store/`**: `DuckDBLake` (`lake.py`) and `SqliteState` (`state.py`), migrations in `store/migrations_duckdb/*.sql` and `store/migrations_sqlite/*.sql` applied in order by `stonks db init`. Bars sit behind the `BarStore` seam (`bars.py`): the DuckDB `bars` table (default) or hive-partitioned Parquet files that stay readable while `stonks serve` holds the lake; `lake_settings.bars_backend` records which. `filelock.py`, `corporate_actions.py` (splits and dividends reads). `SqliteState` is a thin foundation; domain helpers belong to the block that owns each table.
- **`features/`**: optional helper toolkit, no pipeline stage. `library.py` (`ttm`, `rolling_zscore`, `trailing_return`), indicators (ATR), volatility, cross-section, momentum, trend, forecast (Carver EWMAC, TSMOM), fundamentals (value, forensic, quality scores), trailing stop, complexity, extremes, ML seams and bet sizing (`ml.py`), triple-barrier labels and uniqueness (`labels.py`), latent regimes (`regimes.py`, wraps `statsmodels`), the VIX term structure, the `VolForecaster` seam (`vol_forecast.py`: EWMA, GARCH wrapping `arch`, HAR-RV), VSA, market profile, visibility graph, spread, sessions, price adjustment.
- **`strategies/`**: `BaseStrategy` plus 25 examples in `strategies/examples/` (reference, fundamentals, book strategies such as `quant_momentum`, `stocks_on_the_move`, `quant_value`, trend following such as `ewmac_trend`, `tsmom`, `ath_trend`, and the neurotrader888 ports), the wrappers `MacroRegimeFilter`, `FeatureRegimeFilter`, `LatentRegimeFilter` (`latent_regime.py`, a Markov-switching regime), `LastTradeFilter` and `TrailingStopWrapper` (`_wrapping.py`), and the declarative `RuleStrategy` (`rule_based.py`, `rules/`) behind the Strategy Studio. Feature extraction lives inside each strategy. Strategies declare `applicable_asset_classes`; the ranker drops other tickers. Full list: `docs/strategies/README.md`.
- **`portfolio/`**: the construction seam. `PortfolioConstructor` ABC and registry (`base.py`), `signals.py` (normalisation), constructors `single_winner` (default), `equal_weight_top_n`, `inverse_vol`, `vol_target` (`constructors.py`), `atr_parity`, and the optimisers `hrp`, `erc` and `mean_variance_costs` (cvxpy, wrapped) over covariance estimators (`covariance.py`), buffered `orders_from_targets` (`orders.py`), and `pipeline.build_orders`, the one pipeline the tick and the backtest share (signals, constructor, orders, stale-price guard, risk rules, per-strategy attribution).
- **`lab/`**: `runner.py` (preflight, tune, fit, survival suite, verdict, optional register), `catalog.py` (every strategy the lab can name), tuners in `tuning/` (grid, random), `objectives.py`, `trials.py` (trial ledger with hypothesis and premortem, `lab_runs`/`lab_trials`), `manifest.py` (reproducibility), `parallel.py` (the one spawn process pool), `dataset.py` (windows, embargo, `train_segments`), `cv.py` (purged and combinatorial purged k-fold, `CVObjective`), `signal_eval.py` (IC analysis), `backtesting.py`. `survival/registry.py` discovers 22 tests (`benchmark_relative`, `cost_stress`, `cpcv`, `crisis`, `cross_instrument`, `deflated_sharpe`, `drift`, `event_study`, `mc_trades`, `mcpt`, `oos`, `pbo`, `period_stability`, `perturbation`, `plateau`, `pool_correlation`, `runs_test`, `signal_ic`, `stress`, `vs_random`, `walk_forward`, `walk_forward_mcpt`) and names the presets `quick`, `standard` and `promotion`.
- **`stats/`**: PSR, deflated Sharpe, MinTRL, bootstrap, HAC, CSCV/PBO, multiple testing (FDR).
- **`backtest/`**: interval-aware `Backtester` (`engine.py`, runs the construction pipeline when `BacktestConfig.construction` is set), `SimulatedBroker` (idempotent by `client_id`, short sales on margin with borrow fees and debit interest through `accrue`), `fills.py` (participation cap, partial fills, limit/stop fills from the bar range, gap guard), `costs.py` (per-asset-class fee, spread and square-root impact), `trades.py` (FIFO round trips), `metrics.py` (Sharpe, Sortino, Calmar, Ulcer, VaR, ES, ...), `benchmark.py` (benchmark curve, alpha, beta), `corporate_actions.py` (splits and dividends), `calendar.py`, `report.py`.
- **`registry/`**: `StrategyRegistry` over SQLite plus `ArtifactBundle` at `data/artifacts/<id>/` (the stored path is relative to the artifacts folder, so a restore into another data folder still loads). Governance: `set_status` is the only writer of `strategies.status`, every change writes a `status_changes` row, and promotion needs a passing go-live check or an override with a reason.
- **`production/`**: `run_tick` (`tick.py`) in three phases: the signal phase (`ranker.py` scores each active strategy once, `scoring.py` on all cores for opted-in strategies, `signals.py` stores signals and signal events), a portfolio phase over the books of a `TickPlan` (construction pipeline, risk rules, broker, ledger, `portfolio`-stage hooks), then model books (`shadow.py`) and `tick`-stage hooks. The entrypoints build one book per portfolio from its paper and auto subscriptions (`load_tick_plan`, `[production] books_from_subscriptions = true`). `pf_default` follows every active strategy because a promotion subscribes it (`accounts/default_book.py`). Auto books pause on a broker error (`auto_pause.py`) and `portfolio_runs.py` counts paper days. Short books accrue borrow fees and debit interest from a stored date (`financing.py`). `risk.py` plus `rules/` (registered `RiskRule`s: the caps, position risk, portfolio vol, drawdown scaling, liquidity, sector cap, max holding, circuit breaker and operational halt, and for shorts gross and net exposure, short caps, borrow check, squeeze guard and margin call, set under `[production.risk.rules.*]`), live risk (`risk_metrics.py`, `decay.py`: VaR, ES, violation ratio and alpha decay, set under `[production.risk_monitor]` and `[production.decay]`), `halts.py` (`risk_halts` rows: kill switch, breaker trips, the operational halt that `run_health` opens), `quit_rule.py`, `hooks/` (position attribution, notification enqueue, the quit rule, TCA, the risk monitor, and the `risk_halts` trade gate), `golive.py` (incubation gate), `pnl.py`, `health.py`, `corporate_actions.py`, `prices.py`, `settings_builder.py`.
- **`execution/`**: `make_client_id` (`orders.py`), brokers in `brokers/` (`simulated`, `alpaca` wrapping `alpaca-py`, `make_broker`), `reconcile.py` (syncs broker order state and fills into `orders`/`fills`), and for short selling `margin.py` (cash and Reg T margin models) and `borrow.py` (borrow quotes and fees).
- **`accounts/`**: users and roles (viewer, trader, admin), portfolios, subscriptions with modes `notify`/`paper`/`auto`, `BookSpec` with tighten-only merges, `Scope` ownership checks, `audit_log`, paper accounts for broker portfolios (`paper.py`). Existing installs map to `usr_owner` and `pf_default`, and `default_book.py` subscribes `pf_default` to every strategy that turns active (paper, or auto at an external broker).
- **`connections/`**: `BrokerConnection` seam for broker sync (positions, cash, activities) and, for auto books, trading. Providers `alpaca`, `snaptrade`, `fake`, `fake_portal` and `fake_trading`; none enabled by default. Credentials sealed with `security/`.
- **`insights/`**: portfolio insights for any portfolio you own, a synced broker account too: allocation, exposure, P&L, risk, and which active strategies agree with each holding (`/api/insights`, service in `app/insights.py`).
- **`security/`**: AES-GCM envelope encryption (`SecretBox`) with master keys from `STONKS_SECRET_KEYS`.
- **`notify/`**: `Notifier` seam for operator alerts (log, store, webhook) and the per-user notification router, outbox, delivery worker with retries, quiet hours and preferences, and channels (Web Push via VAPID, SMTP email, the user's own webhook).
- **`scheduling/`**: built-in scheduler with exchange calendars, session/daily/interval triggers, catch-up, run records, dead-man deadlines and pings, Prometheus metrics, and `api`, `in_process` and `local` backends.
- **`ops/`**: `backup`, `verify`, `restore`, `list`, `prune` of lake, state and artifacts with retention, plus `restore-snapshot` and `check-restore` for the off-server restore scripts.
- **`reporting/`**: static HTML report, backtest tear sheets, signal research sections.
- **`app/`**: the service layer (`services.py` wires lake, state, registry, lab, backtests, ticks, studio, jobs). No business logic in any transport.
- **`auth/`**: sign-in with passwords, sessions, mandatory TOTP 2FA, recovery codes, API tokens and role permissions (`stonks users`). Every API route declares the permission it needs.
- **`universes/`**: stored universe definitions (list, exchange, rule, index) behind `UniverseProvider` and `IndexSource` registries, refreshed into point-in-time membership. See `docs/universes.md`.
- **`api/`**: FastAPI app (`stonks serve`), session or API-token auth with per-route permissions (`STONKS_API_TOKEN` is a legacy credential), background jobs with SSE, OpenAPI contract, serves `web/dist`.
- **`mcp/`**: `stonks mcp`, an MCP server that talks to the running REST API. Write tools need an explicit confirm.
- **`web/`**: Angular console (dashboard, strategies, lab, studio, data, orders, shadow, go-live, health, settings), typed client generated from the OpenAPI spec, installable PWA. See `docs/ui.md`.
- **Deploy**: `Dockerfile`, `deploy/` (Compose with api, scheduler and Caddy, Tailscale, restic backups, host checks), `infra/` (Terraform), `.github/workflows/` (ci, codeql, docs, release, deploy, mutation).

## Canonical schemas (current)

**Lake (DuckDB, migrations 001-016):**
- `instruments (id, asset_class, exchange, currency, ipo_date, sector, industry, is_delisted, name, identifiers, GICS, address, ...)`: renamed from `tickers` in 007. `asset_class` in {equity, crypto, commodity, bond}.
- `bars (ticker, timestamp, interval, open, high, low, close, adj_close, volume; PK (ticker, timestamp, interval))`: OHLCV at any `Interval` code (1m, 5m, 1h, 4h, 1d, 1w, 1mo, ...). `prices` is a read-only view of `interval='1d'`. Write with `upsert_bars` or the daily `upsert_prices` shim. With the Parquet backend the rows live under `<lake dir>/bars` instead of the table.
- Statements (008, equity only), keyed `(ticker, period_end, frequency)`: `income_statement`, `balance_sheet`, `cash_flow_statement`, each with `filing_date` and `currency`. `upsert_<statement>` reindexes sparse frames and uses `COALESCE(EXCLUDED.col, table.col)`, so a NULL never overwrites a stored value but a real restated value does.
- Equity metadata (002, 004-006, 010): `dividends`, `stock_splits`, `market_cap_history`, `insider_transactions` (natural key), `news`, `news_sentiment`, `analyst_estimates`, `analyst_ratings`, `shares_outstanding`, `employee_count`, `segmentation`.
- Per-class profiles (007): `crypto_profiles`, `bond_profiles`, `bond_yield_history`, `commodity_contracts`. No FK enforcement.
- `macro_indicators (country_iso, indicator, observation_date, period, country_name, value)` (009): ISO alpha-3 country, `lower_snake_case` indicator (open set), `period` in {annual, quarterly, monthly} or NULL.
- `defi_tvl (chain, observation_date, tvl_usd, source)` (011).
- `lake_settings (key, value)` (012): today only `bars_backend`.
- `quarantined_bars (id, run_id, ticker, timestamp, interval, OHLCV, reasons, source, quarantined_at)` and `ingest_runs.quality_json` (013).
- `ingest_runs (id, source, kind, started_at, finished_at, tickers_ok, tickers_failed, status, error, quality_json)`.
- `statement_flags (ticker, period_end, frequency, check_id, severity, detail, flagged_at)` (014): the statement audit's findings, replaced per audited ticker.
- `universe_membership (universe_id, ticker, start_date, end_date)` (015): point-in-time universes, delisted names included.
- `universe_definitions`, `index_constituent_snapshots`, `index_constituent_changes`, `bar_fetch_ranges` (016): stored universe definitions (list, exchange, rule, index), index history, and the bar ranges already requested so on-demand fetches skip them. See `docs/universes.md`.

**State (SQLite, migrations 001-023):**
- 001: `strategies (id, class_path, params_json, artifact_path, status, ...)` with status in {active, shadow, retired}; `survival_reports`; `tick_runs (id ulid, started_at, finished_at, status, summary_json)`; `orders (client_id PK, tick_id, strategy_id, ticker, side, quantity, order_type, limit_price, status, broker_order_id, ...)`; `fills`; `portfolio_snapshots (tick_id, taken_at, cash, positions_json, total_value)`.
- 002: `shadow_decisions`, `shadow_portfolio_snapshots` (model books).
- 003: `jobs` (API background jobs). 004: `portfolio_snapshots.as_of`. 005: `strategy_drafts` (Studio). 006: `orders.status_reason`. 007: `alerts`.
- 008: `lab_runs`, `lab_trials` (trial ledger). 009: `status_changes` (governance audit).
- 010 accounts: `users`, `portfolios`, `subscriptions`, `audit_log`; adds `portfolio_id` to orders, fills and snapshots, `owner_id` to jobs and drafts, `user_id`/`category`/`dedupe_key`/`read_at` to alerts.
- 011 scheduler: `scheduled_runs`, `scheduler_instances`, `scheduler_deadline_alerts`.
- 012 notify: `notification_outbox`, `notification_deliveries`, `push_subscriptions`, `notification_prefs`, `notification_settings`.
- 013 connections: `broker_connections`, `broker_credentials`, `broker_accounts`, `broker_positions`, `broker_activities`; `portfolio_snapshots.source` (`tick` or sync).
- 014: `position_attribution (tick_id, portfolio_id, as_of, ticker, strategy_id, subscription_id, quantity, target_weight, weight_share, source)`.
- 015 auth: `sessions`, `api_tokens`, `recovery_codes`, `login_attempts`, and MFA columns on `users`.
- 016: `risk_halts (id, kind, scope, user_id, portfolio_id, halt, reason, tripped_by, tripped_at, expires_on, cleared_at, cleared_by, clear_reason)`: the kill switch, circuit-breaker trips and the operational halt.
- 017 TCA: decision price, time, context and expected cost on `orders`, `fills.arrival_price`, `journal_notes`. 018: `corporate_action_ledger`, `corporate_action_ledger_start`.
- 019: `risk_snapshots` (daily VaR, ES, violations and decay per book and strategy sleeve).
- 020: `signals`, `signal_events`, `portfolio_runs`, `portfolios.paper_of` (a broker portfolio's paper account), `subscriptions.paper_since`, `users.risk_policy_json`.
- 021: `orders.position_effect` (`open` or `close`). 022: a `pf_default` subscription for every active strategy. 023: `financing_accruals`, `financing_charges`.

## Conventions to match

- Settings are pydantic models (`config.py`, `config/default.toml`, env overrides); never long kwarg lists. Some blocks own their settings models (`scheduling/config.py`, `ops/config.py`, `ingest/quality_config.py`, `ingest/ensure_settings.py`, `connections/settings.py`, `notify/settings.py`, `production/rules/settings.py`).
- ABCs, Protocols and registries are the seams for new behavior (see the third-party rule above).
- Logging via `stonks.logging.get_logger(name)` (structlog JSON). Cross-block actions carry a `run_id` / `tick_id` so logs correlate.
- Free-tier EODHD only returns EOD prices; the fundamentals endpoint returns a text error. Live fundamentals tests skip on that signal, never fail.
- **`Literal` vs `Enum`.** Default to `Literal[...]` for closed sets of string tags that cross serialization boundaries (DB columns, JSON, vendor APIs). Use `StrEnum` when the set grows behavior, needs iteration, or named symbols read better (e.g. `Role`, `Mode`).
- Docs: plain, short English, one topic per page, no em dashes. `docs/api/*` and `docs/mcp.md` are generated.

## Known external limits

- EODHD free tier: prices only, 1-year window at most.
- Treat any ticker-level vendor failure as a soft fail (log and continue); the run row records `tickers_ok` / `tickers_failed`.
- DuckDB allows one writing process per lake file; while `stonks serve` runs, other writers go through the API.
