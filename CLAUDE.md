# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

An end-to-end multi-asset research and trading system: ingest data into a lake, test strategies in a lab, register the survivors, and run a daily production tick that trades one or more portfolios. The CLI, REST API, MCP server and Angular web console all sit on one service layer.

Asset classes (closed set in `core.types.AssetClass`): `equity`, `crypto`, `commodity`, `bond`. Equities are the default and have the richest metadata (statements, dividends, insiders, analysts, ...). Other classes use the same `bars` time series plus small profile tables.

Read first: `docs/architecture.md` (the system), `docs/principles.md` (the research and risk rules every change must respect), `docs/operations.md`, `docs/deploy.md`, `docs/roadmap.md`, `docs/design/accounts-and-modes.md`, `docs/design/intraday.md`, `docs/strategies/README.md`.

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

# Screener and calendars (docs/universes.md#screener, docs/calendars.md)
uv run stonks screener metrics | run --spec JSON [--as-of ...] | save NAME --spec JSON | list | delete ID
uv run stonks screener universe ID (--spec JSON|--screen ID) [--mode rule|snapshot]   # a screen as a universe
uv run stonks calendars show|news [--scope holdings|watchlists|tickers|all] | earnings-check TICKERS
uv run stonks calendars refresh [--source eodhd] [--no-alerts]   # also the daily calendars_refresh job

# Lab and registry
uv run stonks lab run momentum --start 2023-01-01 --end 2025-01-01 --tickers AAPL.US,MSFT.US --preset promotion
uv run stonks lab run ... --strict | --no-preflight   # data preflight: warnings as errors, or skip it
uv run stonks lab run ... --universe-id ID [--ensure-data]   # a stored universe, fetching missing bars first
uv run stonks lab sweep --start 2023-01-01 --end 2025-01-01
uv run stonks lab ic --strategy momentum --tickers AAPL.US [--events]   # signal IC and event study
uv run stonks lab run ... --objective cv_sharpe   # score trials on purged folds (also cv_cagr, cv_final_return)
uv run stonks lab run ... --tuner optuna [--sampler tpe|nsga2|random --prune] --heatmap auto|x,y   # Bayesian search, parameter heatmap
uv run stonks registry list [--status active|shadow|retired]
uv run stonks registry show|history <id>
uv run stonks registry promote <id> [--override --reason "..."]
uv run stonks registry shadow|retire <id> --reason "..."
uv run stonks registry versions|version-history <id> | candidates   # model versions (docs/model-lifecycle.md)
uv run stonks registry retrain [<id>...] | swap-check|swap|reject <id> VERSION   # refit into a candidate, governed swap
uv run stonks golive check <id>

# Factors (docs/factors.md)
uv run stonks factors list [--set alpha158|classic|fundamentals] | show ID | check 'FORMULA'
uv run stonks factors values FACTOR --as-of YYYY-MM-DD --tickers ...|--universe-id ID
uv run stonks factors tearsheet FACTOR --start ... --end ... --universe-id ID [--html F --json F]
uv run stonks factors dataset alpha158 --start ... --end ... --universe-id ID --out data.parquet

# Production
uv run stonks tick [--dry-run] [--as-of YYYY-MM-DD] [--tickers AAPL.US,MSFT.US]
uv run stonks health [--notify]   # also opens or clears the global operational halt
uv run stonks halts list [--all]
uv run stonks halts kill --scope global|user|portfolio [--portfolio ID] [--buys-only] --reason "..."
uv run stonks halts resume ID --reason "..."   # asks you to type RESUME TRADING
uv run stonks halts clear ID --reason "..."    # circuit-breaker or operational halt
uv run stonks reconcile list [--portfolio ID] | show ID | run --portfolio ID [--kind adhoc|sod|submit|eod]   # broker vs ledger drift
uv run stonks halts drill [--json-out F]       # kill switch dry run: scratch state, simulated broker
uv run stonks live soak-report --portfolio ID [--days 20 --strict]   # paper soak summary
uv run stonks live reconcile --portfolio ID    # sync open orders with the broker now (read only)
uv run stonks pnl [--since YYYY-MM-DD] [--strategy <shadow-id>] [--portfolio ID]
uv run stonks report [--backtest <job-or-strategy> --start ... --end ...]
uv run stonks tca summary|journal|order|note|edit-note|refresh   # transaction costs and the trade journal
uv run stonks journal trades|show|review|calendar|breakdown|playbooks|playbook-add|playbook-edit|labels   # round trips, R, P&L calendar (docs/journal.md)
uv run stonks tca intraday [--by order|sleeve|...] | calibrate --interval 1m --end YYYY-MM-DD [--out F]   # intraday TCA, proposed cost block (never applied)
uv run stonks options ingest|chain|strategies|backtest [--validate]   # options research (Phase 17), nothing trades
uv run stonks orders place|preview|change|cancel|list [--user E]   # manual orders through every check
uv run stonks tickets list|show|approve|reject [--user E]   # order tickets; approve asks you to type APPROVE TICKETS
uv run stonks live stage show|report|promote|demote PORTFOLIO [--to STAGE --reason "..."]   # live stages and gates
uv run stonks live preview PORTFOLIO   # the live book's next orders through the broker's what-if, never sent
uv run stonks price-alerts list|create|delete|events --user E | run   # price alerts, run = the scheduler job
uv run stonks telegram link-code|status|unlink --user E | poll [--once]
uv run stonks tax gains|dividends --year Y [--portfolio ID] | lots [--as-of D] | settings   # tax CSVs, see docs/tax.md
uv run stonks ingest fx --pairs EURUSD,GBPUSD [--since ...]   # FX rates into the lake
uv run stonks ingest borrow [--markets usa,uk]   # IBKR short stock files into borrow_rates
uv run stonks cash-flows record|list --user E --portfolio ID   # deposits and withdrawals (TWR, MWR)
uv run stonks assistant eval [--base-url URL --model M]   # the assistant's eval set

# Servers
uv run stonks serve              # REST API + built console on 127.0.0.1:8000
uv run stonks mcp                # MCP server over the running API

# Operations
uv run stonks schedule run|next|runs|run-now JOB|check|metrics
uv run stonks backup backup|verify|restore|list|prune

# People (the shell is admin, and passwords come from a no-echo prompt)
uv run stonks users bootstrap|reset-password|list
uv run stonks users create --email E --name N [--role viewer|trader|admin]
uv run stonks users set-role|disable|enable|reset-2fa --email E   # reset-2fa: sole-admin lockout

# Operator entry points
uv run python -m stonks.notify vapid-keygen|test --user EMAIL|deliver
uv run python -m stonks.connections providers|list|connect|sync [--due]|...
uv run python -m stonks.security keygen
uv run python -m stonks.streaming sources|run|record|replay   # live streams into 1m bars (Phase 21.1, off by default)
uv run python -m stonks.engine run [--session D] | replay PATH [--write-bars] | status | stop   # the intraday engine (Phase 21, off by default)
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
- **Third-party libraries.** Prefer well-maintained libraries over hand-rolled trading or finance logic, but wrap every non-trivial one behind a seam (`DataSource`, `Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`, `BrokerConnection`, `PortfolioConstructor`, `RiskRule`, `BarStore`, a notification `Channel`, or a new ABC) so vendor types never leak into `core/` or other blocks. Examples: `yfinance` in `ingest/sources/yahoo.py`, `alpaca-py` in `execution/brokers/alpaca.py`, `exchange_calendars` in `scheduling/calendar.py`, `pywebpush` in `notify/webpush.py`, `cryptography` in `security/crypto.py`, `websockets` in `streaming/sources/eodhd_ws.py`. numpy, pandas and scipy are exempt.
- **Registries, not lists.** Survival tests, portfolio constructors, risk rules, tick hooks, strategies and factors are discovered from their packages. A new one is one new module; no central list is edited.

## Architecture in one screen

- **`core/`**: dependency-free primitives. `types.py` (Bar, Order, Fill, Portfolio, AssetClass), `interval.py`, `params.py` (`ParameterSpec`, `Params`, `ParamSpace`), `protocols.py` (`Strategy`, `Tuner`, `Objective`, `SurvivalTest`, `Broker`), `corporate_actions.py`, `timeutil.py`, `clock.py` (the `Clock` seam: system, fixed, fake), `stream.py` (vendor-neutral stream events: `TradeTick`, `QuoteTick`, `StreamBar`, `Heartbeat`).
- **`ingest/`**: `DataSource` ABC and `IngestPipeline` (normalize, validate, idempotent upsert, one `ingest_runs` row per run). Sources in `ingest/sources/`: `eodhd`, `yahoo` (wraps `yfinance`), `defillama` (DeFi TVL), picked by `--source` through `sources/registry.py`, plus `ibkr_borrow` (IBKR's public short stock files, `stonks ingest borrow`, not a `--source`). `quality.py` checks every bar batch and moves bad rows to `quarantined_bars`; the pipeline can retry a failed ticker on a fallback source.
- **`streaming/`** (Phase 21.1, off by default, `[streaming]`): the `StreamingSource` seam (`base.py`) and registry discovered from `sources/` (`eodhd_ws` wraps `websockets` for EODHD's feeds, `ibkr` polls the IBKR adapter's quotes under client id 14, `replay` plays a recording back and can drive a `FakeClock`), `bars.py` (`BarBuilder`: ticks to 1m bars with the `bars` columns, late ticks dropped), `writer.py` (`BarWriter`: idempotent upserts into the `BarStore`), `recorder.py` (Parquet recordings through DuckDB), `runner.py` (`StreamRunner`: reconnect with backoff, stale detection in market hours, gap backfill through the REST intraday ingest, subscribers), `health.py` (Prometheus families). Entry point `python -m stonks.streaming`. See `docs/design/intraday.md`.
- **`engine/`** (Phase 21.2 and 21.3, off by default, `[engine]` with `[[engine.books]]`): the intraday event engine. `driver.py` (`EventDriver`: any `StreamingSource` to bar closes, `FakeClock` hand-off, handlers by priority), `step.py` (`DecisionStep`: decide on each bar close through a minute `PointInTimeLake` and `build_orders` per book, session gates and flatten, `decision_context.interval` on every order), `sessions.py` (session rules, trading halts, the order gate, flatten orders), `router.py` (`IntradayRouter`: day orders through the order state machine, startup reconcile, per-event reconcile) and `sim_broker.py` (`IntradaySimBroker`: next-bar minute fills), `process.py` (`EngineProcess`, `build_engine`: the always-on loop over the stream runner or a replay, restart from the ledger without re-sending, and on every bar close intraday P&L, the book's `IntradayContext` for the intraday risk rules, the `intraday_loss` and `runaway` halts, the halts in force with cancels on a stop-all kill switch), `recovery.py` (`engine_runs`, crash recovery), `control.py` (lock and stop files), `monitor.py` (`EngineMonitor`: dispatch lag, event to order latency, the `engine_status` row in `status.py`), `deadman.py` (no bar close in market hours alerts), `settings.py`. Jobs `engine_start` and `engine_stop` on all three scheduler backends. The intraday backtest is `backtest/intraday.py` on the same driver and step; intraday P&L is `production/intraday_pnl.py` (`[production.intraday_pnl]`), intraday risk `production/rules/intraday_*.py` and `production/intraday_halts.py`, intraday TCA `production/intraday_tca.py` with `backtest/cost_calibration.py`. The console's Live page shows the engine and each portfolio's intraday P&L. See `docs/design/intraday.md`.
- **`store/`**: `DuckDBLake` (`lake.py`) and `SqliteState` (`state.py`), migrations in `store/migrations_duckdb/*.sql` and `store/migrations_sqlite/*.sql` applied in order by `stonks db init`. Bars sit behind the `BarStore` seam (`bars.py`): the DuckDB `bars` table (default) or hive-partitioned Parquet files that stay readable while `stonks serve` holds the lake; `lake_settings.bars_backend` records which. `filelock.py`, `corporate_actions.py` (splits and dividends reads). `SqliteState` is a thin foundation; domain helpers belong to the block that owns each table.
- **`features/`**: optional helper toolkit, no pipeline stage. `library.py` (`ttm`, `rolling_zscore`, `trailing_return`), indicators (ATR), volatility, cross-section, momentum, trend, forecast (Carver EWMAC, TSMOM), fundamentals (value, forensic, quality scores), trailing stop, complexity, extremes, ML seams and bet sizing (`ml.py`), triple-barrier labels and uniqueness (`labels.py`), latent regimes (`regimes.py`, wraps `statsmodels`), the VIX term structure, the `VolForecaster` seam (`vol_forecast.py`: EWMA, GARCH wrapping `arch`, HAR-RV), VSA, market profile, visibility graph, spread, sessions, price adjustment.
- **`factors/`**: the `Factor` seam (`base.py`) and a registry discovered from `library/` (one module per set: `alpha158`, `classic`, `fundamentals`), a Qlib-style expression language (`expression.py`) compiled to DuckDB window SQL (`sql.py`), point-in-time panels (`engine.py`: as-of adjustment, membership), the panel cache keyed by a data fingerprint (`cache.py`, `panels.py`, `[factors]` in `settings.py`), tear sheets (`tearsheet.py`: IC by horizon, sector, asset class and size, quantile returns, alpha and beta, monthly IC), model datasets with a next-open label (`dataset.py`), and style exposures and style factor returns (`style.py`: momentum, size, value, volatility, sector) for the factor risk model. Service in `app/factors.py`, `stonks factors`, `/api/factors`.
- **`strategies/`**: `BaseStrategy` plus 32 examples in `strategies/examples/` (reference, fundamentals, three intraday strategies (`intraday_orb`, `intraday_vwap_reversion`, `intraday_momentum`), `factor` over any factor or formula, book strategies such as `quant_momentum`, `stocks_on_the_move`, `quant_value`, trend following such as `ewmac_trend`, `tsmom`, `ath_trend`, and the neurotrader888 ports), the wrappers `MacroRegimeFilter`, `FeatureRegimeFilter`, `LatentRegimeFilter` (`latent_regime.py`, a Markov-switching regime), `LastTradeFilter` (`_wrapping.py`) and `TrailingStopWrapper` (`trailing_stop.py`), 38 catalog entries in all (`lab/catalog.py`), and the declarative `RuleStrategy` (`rule_based.py`, `rules/`) behind the Strategy Studio. Feature extraction lives inside each strategy. Strategies declare `applicable_asset_classes`; the ranker drops other tickers. Full list: `docs/strategies/README.md`.
- **`portfolio/`**: the construction seam. `PortfolioConstructor` ABC and registry (`base.py`), `signals.py` (normalisation), constructors `single_winner` (default), `equal_weight_top_n`, `inverse_vol`, `vol_target` (`constructors.py`), `atr_parity`, and the optimisers `hrp`, `erc` and `mean_variance_costs` (cvxpy, wrapped) over covariance estimators (`covariance.py`, including the `pca` and `style` factor risk models of `factor_model.py`), buffered `orders_from_targets` (`orders.py`), and `pipeline.build_orders`, the one pipeline the tick and the backtest share (signals, constructor, orders, stale-price guard, risk rules, per-strategy attribution).
- **`lab/`**: `runner.py` (preflight, tune, fit, survival suite, verdict, optional register), `catalog.py` (every strategy the lab can name), tuners in `tuning/` (grid, random, optuna wrapped in `tuning/optuna.py`), `objectives.py` (sharpe, cagr, final_return, sortino, calmar, sharpe_dd, multi), `heatmap.py` (2D parameter sweeps with the plateau verdict), `trials.py` (trial ledger with hypothesis and premortem, `lab_runs`/`lab_trials`), `manifest.py` (reproducibility), `parallel.py` (the one spawn process pool), `dataset.py` (windows, embargo, `train_segments`), `cv.py` (purged and combinatorial purged k-fold, `CVObjective`), `signal_eval.py` (IC analysis), `backtesting.py`. `survival/registry.py` discovers 22 tests (`benchmark_relative`, `cost_stress`, `cpcv`, `crisis`, `cross_instrument`, `deflated_sharpe`, `drift`, `event_study`, `mc_trades`, `mcpt`, `oos`, `pbo`, `period_stability`, `perturbation`, `plateau`, `pool_correlation`, `runs_test`, `signal_ic`, `stress`, `vs_random`, `walk_forward`, `walk_forward_mcpt`) and names the presets `quick`, `standard` and `promotion`.
- **`stats/`**: PSR, deflated Sharpe, MinTRL, bootstrap, HAC, CSCV/PBO, multiple testing (FDR).
- **`backtest/`**: interval-aware `Backtester` (`engine.py`, runs the construction pipeline when `BacktestConfig.construction` is set), `SimulatedBroker` (idempotent by `client_id`, short sales on margin with borrow fees and debit interest through `accrue`), `fills.py` (participation cap, partial fills, limit/stop fills from the bar range, gap guard), `costs.py` (per-asset-class fee, spread and square-root impact), `trades.py` (FIFO round trips), `metrics.py` (Sharpe, Sortino, Calmar, Ulcer, VaR, ES, ...), `benchmark.py` (benchmark curve, alpha, beta), `corporate_actions.py` (splits and dividends), `calendar.py`, `report.py`.
- **`lifecycle/`**: model versions for strategies that learn from data (`[lifecycle]`, `[lifecycle.swap]`). `retrain.py` refits into candidate versions that run as model books (the weekly `model_retrain` job), `check.py` is the swap check against the live version. A swap is audited and governed. See `docs/model-lifecycle.md`.
- **`registry/`**: `StrategyRegistry` over SQLite plus `ArtifactBundle` at `data/artifacts/<id>/` (the stored path is relative to the artifacts folder, so a restore into another data folder still loads). Governance: `set_status` is the only writer of `strategies.status`, every change writes a `status_changes` row, and promotion needs a passing go-live check or an override with a reason.
- **`production/`**: `run_tick` (`tick.py`) in three phases: the signal phase (`ranker.py` scores each active strategy once, `scoring.py` on all cores for opted-in strategies, `signals.py` stores signals and signal events), a portfolio phase over the books of a `TickPlan` (construction pipeline, risk rules, broker, ledger, `portfolio`-stage hooks), then model books (`shadow.py`) and `tick`-stage hooks. The entrypoints build one book per portfolio from its paper, approve and auto subscriptions (`load_tick_plan`, `[production] books_from_subscriptions = true`). `pf_default` follows every active strategy because a promotion subscribes it (`accounts/default_book.py`). Auto books pause on a broker fault, while a broker outage only skips the day (`auto_pause.py`), and `portfolio_runs.py` counts paper days. Short books accrue borrow fees and debit interest from a stored date (`financing.py`). `risk.py` plus `rules/` (registered `RiskRule`s: the caps, position risk, portfolio vol, style exposure, drawdown scaling, liquidity, sector cap, max holding, circuit breaker and operational halt, and for shorts gross and net exposure, short caps, borrow check, squeeze guard and margin call, set under `[production.risk.rules.*]`), live risk (`risk_metrics.py`, `decay.py`: VaR, ES, violation ratio and alpha decay, set under `[production.risk_monitor]` and `[production.decay]`), intraday marks and P&L (`intraday_pnl.py`: `MarkBook`, `IntradayPnlTracker`, `intraday_snapshots`), `halts.py` (`risk_halts` rows: kill switch, breaker trips, the operational halt that `run_health` opens), `quit_rule.py`, `hooks/` (position attribution, notification enqueue, the quit rule, TCA, the risk monitor, and the `risk_halts` trade gate), `golive.py` (incubation gate), `intraday_tca.py` (21.3.5: intraday shortfall with arrival at the next minute and spreads from recorded quotes, and the evidence for `backtest/cost_calibration.py`, which proposes a `[backtest.costs]` block), `live/` (Phase 19: the live context, allocation, quotes, runaway, and for 19.9 `stages.py` (the stage state machine), `gates.py` (daily gate metrics, gate reports, the `DriftSource` and `ModelBook` seams) and `preview.py` (a dry run through the broker's what-if that never transmits)), `pnl.py`, `health.py`, `corporate_actions.py`, `prices.py`, `settings_builder.py`. Live books (`live/`): the live context, tickets and the submit window (`tickets.py`, `submit.py`), and optional broker-side protective stops (`live/stops.py`, `[production.risk.rules.protective_stops]`, off by default: one GTC stop per own position, resized and cancelled with it, OCA group shared with its exits, synced by the tick and the `live_stops` job, resting in the simulated broker for paper books).
- **`execution/`**: `make_client_id` (`orders.py`), brokers in `brokers/` (`simulated`, `alpaca` wrapping `alpaca-py`, `make_broker`), `reconcile.py` (syncs broker order state and fills into `orders`/`fills`), `drift.py` (broker versus ledger diffs for Stonks' own positions and orders, run by the checks in `production/live/checks.py`: start of day, submit, end of day, stored in `reconcile_reports`, drift opens the `broker_drift` halt and pauses auto), and for short selling `margin.py` (cash and Reg T margin models) and `borrow.py` (borrow quotes and fees, `LakeBorrowSource` over `borrow_rates`). The IBKR adapter is `brokers/ibkr/` (`IbkrBroker` over the `IbClient` protocol, `borrow.py` with `IbkrBorrowSource` from the shortable ticks, `flex.py` for Flex statements).
- **`accounts/`**: users and roles (viewer, trader, admin), portfolios, subscriptions with modes `notify`/`paper`/`approve`/`auto`, `BookSpec` with tighten-only merges, `Scope` ownership checks, `audit_log`, paper accounts for broker portfolios (`paper.py`). Existing installs map to `usr_owner` and `pf_default`, and `default_book.py` subscribes `pf_default` to every strategy that turns active (paper, or auto at an external broker).
- **`connections/`**: `BrokerConnection` seam for broker sync (positions, cash, activities) and, for auto books, trading. Providers `alpaca`, `snaptrade`, `ibkr` (an IB Gateway named in `[brokers.ibkr.gateways]`, sync plus `trader()`, optional Flex activities), `fake`, `fake_portal` and `fake_trading`; none enabled by default. Credentials sealed with `security/`.
- **`insights/`**: portfolio insights for any portfolio you own, a synced broker account too: allocation, exposure, P&L, risk, and which active strategies agree with each holding (`/api/insights`, service in `app/insights.py`).
- **`security/`**: AES-GCM envelope encryption (`SecretBox`) with master keys from `STONKS_SECRET_KEYS`.
- **`notify/`**: `Notifier` seam for operator alerts (log, store, webhook) and the per-user notification router, outbox, delivery worker with retries, quiet hours and preferences, and channels (Web Push via VAPID, SMTP email, the user's own webhook).
- **`scheduling/`**: built-in scheduler with exchange calendars, session/daily/interval triggers, catch-up, run records, dead-man deadlines and pings, Prometheus metrics, and `api`, `in_process` and `local` backends.
- **`ops/`**: `backup`, `verify`, `restore`, `list`, `prune` of lake, state and artifacts with retention, plus `restore-snapshot` and `check-restore` for the off-server restore scripts.
- **`options/`** (Phase 17, research only, off by default): `PricingModel` seam over QuantLib (`pricing/`), chains and the lake store (`chain.py`, `store.py`, `ingest.py`, `synthetic.py`), risk analytics (`risk.py`: Greeks, max loss, Reg T and risk-based margin), `AssignmentModel`, combo orders, the leg selector, the structure registry (`structures/`) and the options strategies (`strategies/`, own catalog). Contracts live in `core/options.py` and are one case of the general `InstrumentSpec` (`core/instruments.py`); multi-leg parent orders are `core/combos.py`; the options backtest in `backtest/options_*.py`, the option rules in `production/rules/option_*.py` and `short_option_guard.py`. See `docs/design/options.md`.
- **`reporting/`**: static HTML report, backtest tear sheets, signal research sections, factor tear sheets (`factors.py`), factor attribution of P&L (`factor_attribution.py`).
- **`app/`**: the service layer (`services.py` wires lake, state, registry, lab, backtests, ticks, studio, jobs). No business logic in any transport.
- **`auth/`**: sign-in with passwords, sessions, mandatory TOTP 2FA, recovery codes, API tokens and role permissions (`stonks users`). Every API route declares the permission it needs.
- **`universes/`**: stored universe definitions (list, exchange, rule, index) behind `UniverseProvider` and `IndexSource` registries, refreshed into point-in-time membership. See `docs/universes.md`.
- **`screener/`**: `ScreenSpec` (the rule filters plus metric bounds, sort and top N), `ScreenMetric` seam and registry (`metrics/`: price, returns, volatility, valuation and quality), `ScreenData` (point-in-time lake reads), `run_screen`. A `rule` universe's spec is a screen, run at each rebalance. Service in `app/screener.py`.
- **`calendars/`**: `CalendarStore` over the calendar tables plus news reads, `timing.py` (earnings before the next open), `tracking.py` (held and watched tickers), upcoming-event alerts (`alerts.py`, `alert_kinds/` registry). Ingest via `IngestPipeline.run_calendars` and the EODHD adapter `ingest/sources/eodhd_calendar.py`. Service in `app/calendars.py`. See `docs/calendars.md`.
- **`api/`**: FastAPI app (`stonks serve`), session or API-token auth with per-route permissions (`STONKS_API_TOKEN` is a legacy credential), background jobs with SSE, OpenAPI contract, serves `web/dist`.
- **`mcp/`**: `stonks mcp`, an MCP server that talks to the running REST API. Write tools need an explicit confirm.
- **Phase 20 blocks**: `production/manual.py` (manual orders through the gates, every risk rule and the broker, idempotent by client id, `origin = manual`; the tick never trades manual holdings), `price_alerts/` (rules on tickers or watchlists, the `price_alerts` scheduler job, delivery through the notification router), `telegram/` (the `telegram` channel and a long-polling bot acting as the linked user, env token only), `assistant/` (the `ChatModel` seam, an OpenAI-compatible client for Ollama, vLLM or llama.cpp, an agent loop over the in-process MCP tools as the signed-in user, a tool catalog with a small default set, the safety gate and freeze in `guard.py`, the research loop in `research.py` (proposals under a trial and compute budget, validation after the model's cutoff, never registering), and an eval set in `evals.py`), `production/order_drafts.py` (the assistant only drafts orders, approved in the web app with a fresh second factor), `fx/` (conversion over the lake's `fx_rates`) and `tax/` (FIFO or specific lots, US wash sales, dividends, yearly CSVs).
- **`web/`**: Angular console (dashboard, strategies, lab, studio, data, orders, shadow, go-live, health, live engine, settings), typed client generated from the OpenAPI spec, installable PWA. See `docs/ui.md`.
- **Deploy**: `Dockerfile`, `deploy/` (Compose with api and Caddy, profiles `scheduler`, `lab-worker`, `backup`, `ibkr-paper`, `ibkr-live` and `ai`, IB Gateway secrets in `deploy/ibkr/`, Tailscale, restic backups, host checks), `infra/` (Terraform), `.github/workflows/` (ci, codeql, docs, e2e, soak, release, deploy, mutation). Release steps: `CONTRIBUTING.md` and `docs/release-1.0.md`.

## Canonical schemas (current)

**Lake (DuckDB, migrations 001-022):**
- `instruments (id, asset_class, exchange, currency, ipo_date, sector, industry, is_delisted, name, identifiers, GICS, address, ...)`: renamed from `tickers` in 007. `asset_class` in {equity, crypto, commodity, bond}.
- `bars (ticker, timestamp, interval, open, high, low, close, adj_close, volume; PK (ticker, timestamp, interval))`: OHLCV at any `Interval` code (1m, 5m, 1h, 4h, 1d, 1w, 1mo, ...). `prices` is a read-only view of `interval='1d'`. Write with `upsert_bars` or the daily `upsert_prices` shim. With the Parquet backend the rows live under `<lake dir>/bars` instead of the table.
- Statements (008, equity only), keyed `(ticker, period_end, frequency)`: `income_statement`, `balance_sheet`, `cash_flow_statement`, each with `filing_date` and `currency`. `upsert_<statement>` reindexes sparse frames and uses `COALESCE(EXCLUDED.col, table.col)`, so a NULL never overwrites a stored value but a real restated value does.
- Equity metadata (002, 004-006, 010): `dividends`, `stock_splits`, `market_cap_history`, `insider_transactions` (natural key), `news`, `news_sentiment`, `analyst_estimates`, `analyst_ratings`, `shares_outstanding`, `employee_count`, `segmentation`.
- Per-class profiles (007): `crypto_profiles`, `bond_profiles`, `bond_yield_history`, `commodity_contracts`. No FK enforcement.
- `macro_indicators (country_iso, indicator, observation_date, period, country_name, value)` (009): ISO alpha-3 country, `lower_snake_case` indicator (open set), `period` in {annual, quarterly, monthly} or NULL.
- `defi_tvl (chain, observation_date, tvl_usd, source)` (011).
- `fx_rates (base_currency, quote_currency, observation_date, rate, source)` (019): daily FX closes, `rate` = quote units per one base unit. Read through `stonks.fx.FxRates` (latest on or before the day, inverse pair, cross through USD). See `docs/tax.md`.
- `lake_settings (key, value)` (012): today only `bars_backend`.
- `quarantined_bars (id, run_id, ticker, timestamp, interval, OHLCV, reasons, source, quarantined_at)` and `ingest_runs.quality_json` (013).
- `ingest_runs (id, source, kind, started_at, finished_at, tickers_ok, tickers_failed, status, error, quality_json)`.
- `statement_flags (ticker, period_end, frequency, check_id, severity, detail, flagged_at)` (014): the statement audit's findings, replaced per audited ticker.
- `universe_membership (universe_id, ticker, start_date, end_date)` (015): point-in-time universes, delisted names included.
- `universe_definitions`, `index_constituent_snapshots`, `index_constituent_changes`, `bar_fetch_ranges` (016): stored universe definitions (list, exchange, rule, index), index history, and the bar ranges already requested so on-demand fetches skip them. See `docs/universes.md`.
- `option_contracts (contract_id, underlying, expiry, strike, right, style, multiplier, settlement, ...)` and `option_quotes (contract_id, as_of, source, bid, ask, last, volume, open_interest, underlying_price, vendor_iv, vendor_delta..vendor_rho)` (017): option chains, vendor-agnostic. The contract id is `<underlying>:<expiry>:<C|P>:<strike>[:<multiplier>]`.
- `income_statement_versions`, `balance_sheet_versions`, `cash_flow_statement_versions` (018): every version of a statement row with `known_at`, the time Stonks first saw it. Point-in-time reads pick the version known at the decision, so a restatement cannot leak backward (P12).
- `earnings_calendar (ticker, period_end, report_date, before_after_market, eps_estimate, eps_actual, ...)`, `dividend_calendar (ticker, ex_date, amount, record_date, pay_date, ...)`, `economic_events (country, event_time, event_type, comparison, actual, previous, estimate, ...)` (020): event calendars, vendor neutral. See `docs/calendars.md`.
- `borrow_rates (ticker, as_of, source, currency, isin, available_shares, fee_rate_annual, rebate_rate_annual; PK (ticker, as_of, source))` (021): daily stock borrow terms, rates as yearly fractions. Read through `execution.borrow.LakeBorrowSource`.
- `instrument_sector_versions (ticker, sector, gic_sector, known_at)` (022): every sector label an instrument has had, with the time Stonks first saw it. Factor attribution reads the label known on each day (`factors.style.sector_labels`).

**State (SQLite, migrations 001-048):**
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
- 024: reserved (no change). 025: lab offload queue (`jobs.executor`, `lab_workers`). 026: `onboarding_steps`, `onboarding_status` (first-run guide) and `watchlists` (per-user ticker lists).
- 027: `orders.origin` (`strategy` or `manual`), `manual_reason`, `placed_by`, `replaces_client_id`; `price_alert_rules`, `price_alert_state`, `price_alert_events`; `telegram_links`, `telegram_link_codes`, `telegram_bot_state`; `assistant_conversations`, `assistant_messages`, `assistant_pending_actions`; `portfolio_tax_settings`, `tax_lot_picks`; `order_drafts`, `assistant_turns`, `assistant_freezes`, `portfolio_cash_flows`.
- 028 live broker: `orders.state`, `broker_ref`, `stop_price`, `time_in_force`, `outside_rth`; `fills.broker_exec_id`, `fee_currency`, `fee_fx_rate`; halt kinds `runaway` and `broker_drift`; `live_allocations`, `broker_gateway_status`, `account_profiles`, `settlement_ledger`, `account_restricted`, `product_documents`.
- 029: `broker_contracts` (IBKR conId cache).
- 030 research loop: `lab_runs.family`, `research_sessions`, `research_proposals` (roadmap 22.9).
- 031: `screens` (per-user saved screener specs).
- 032 model lifecycle: `model_versions`, `model_version_events` (append only), `model_version_decisions`, `model_version_snapshots` (roadmap 22.6).
- 033: `order_tickets` (append only) and `approve` as a subscription mode (roadmap 19.8).
- 034: `reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status, items_json, explained_json, external_json, summary_json, detail, halt_id, paused_json)`: each check of a live portfolio against its broker, kind in {sod, submit, eod, adhoc}, status in {clean, warn, drift, outage, fault} (roadmap 19.5).
- 035: notification categories `price_alert` and `event_alert` on `alerts`, `notification_outbox` and `notification_prefs` (tables rebuilt), and `event_alert_prefs (user_id, topic, enabled)`: per-person switches for `earnings`, `dividends` and `economic` event alerts, on when no row.
- 036: `orders.oca_group` and `orders.protective` (protective stops, roadmap 19.10).
- 037: `portfolios.live_stage` (`sim_paper`, `broker_paper`, `live_small`, `live_scale`), `live_stage_changes` (append only, a trigger refuses an unlogged stage write) and `live_gate_days` (roadmap 19.9).
- 038: `economic_alert_prefs (user_id, countries_json, min_importance, updated_at)` (roadmap 20.9).
- 039: `order_tickets.hold` also takes `hard_to_borrow` (table rebuilt, roadmap 19.16).
- 040: `account_profiles` loses `jurisdiction` and `base_currency`: they live once, in `portfolio_tax_settings.jurisdiction` and `portfolios.base_currency`, and existing rows are reconciled (the newer wins). `wash_sale_mode` only acts when `portfolio_tax_settings.wash_sales` is on.
- 041: `engine_runs` (the intraday engine process: status, heartbeat, checkpoint, counts, the run it recovered from, roadmap 21.2.5).
- 042: halt kind `intraday_loss` on `risk_halts` (rebuilt with `reconcile_reports`, ids and counter kept, roadmap 21.3.2).
- 043: `intraday_snapshots (portfolio_id, strategy_id, day, at, start_value, value, realised, unrealised, fees, pnl, day_return, high_water_pnl, drawdown, gross_exposure, net_exposure, exposures_json, fills, unmarked, stale_marks, max_mark_age_seconds)`: intraday P&L per book and strategy sleeve every few minutes, unique on `(portfolio_id, strategy_id, at)` (roadmap 21.3.3).
- 044: `engine_status (engine_id, calendar, state, started_at, updated_at, stopped_at, last_dispatch_at, snapshot_json)`: the engine monitor's status row, read by `/metrics`, `/api/stream/status` and the engine dead-man (roadmap 21.3.4).
- 045: `margin_checks (portfolio_id, checked_at, source, currency, equity, initial_margin, maintenance_margin, excess_liquidity, available_funds, buying_power, cushion, level, reported_type)`: each read of a margin account's cushion by the tick or the `live_margin` job, level in {ok, warn, reduce, call} (roadmap 19.13, margin accounts, off by default).
- 046: `option_approvals` (per-portfolio options approval level), `option_events` (assignments, exercises, expiries, append only), and `order_tickets.hold` also takes `options` (roadmap 17.8).
- 047: `shadow_decisions` and `model_version_decisions` statuses gain `working` and `expired`, plus `filled_on` (paper and model books fill at the next open).
- 048: `journal_playbooks`, `trade_annotations (portfolio_id, trade_id = opening fill id, playbook_id, followed_plan, review)` and `trade_labels` (tags and mistakes): the round-trip journal (roadmap 23.3, `journal/`, `docs/journal.md`).

## Conventions to match

- Settings are pydantic models (`config.py`, `config/default.toml`, env overrides); never long kwarg lists. Some blocks own their settings models (`scheduling/config.py`, `ops/config.py`, `ingest/quality_config.py`, `ingest/ensure_settings.py`, `connections/settings.py`, `notify/settings.py`, `production/rules/settings.py`, `factors/settings.py`, `streaming/settings.py`, `engine/settings.py`, `production/intraday_pnl_settings.py`, `production/live/settings.py`, `execution/brokers/ibkr/settings.py`, `portfolio/settings.py`, `lab/offload/settings.py`, `screener/settings.py`, `lifecycle/settings.py`, `assistant/settings.py`, `telegram/settings.py`).
- ABCs, Protocols and registries are the seams for new behavior (see the third-party rule above).
- Logging via `stonks.logging.get_logger(name)` (structlog JSON). Cross-block actions carry a `run_id` / `tick_id` so logs correlate.
- Free-tier EODHD only returns EOD prices; the fundamentals endpoint returns a text error. Live fundamentals tests skip on that signal, never fail.
- **`Literal` vs `Enum`.** Default to `Literal[...]` for closed sets of string tags that cross serialization boundaries (DB columns, JSON, vendor APIs). Use `StrEnum` when the set grows behavior, needs iteration, or named symbols read better (e.g. `Role`, `Mode`).
- Docs: plain, short English, one topic per page, no em dashes. `docs/api/*` and `docs/mcp.md` are generated.

## Known external limits

- EODHD free tier: prices only, 1-year window at most.
- Treat any ticker-level vendor failure as a soft fail (log and continue); the run row records `tickers_ok` / `tickers_failed`.
- DuckDB allows one writing process per lake file; while `stonks serve` runs, other writers go through the API.
