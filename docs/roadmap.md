# Roadmap

Stonks is a solid research engine with a simulated trading loop. This roadmap takes it to paper trading against a real broker, then to better strategies and operational maturity. Each work package (WP) lists the files it owns so packages can be built in parallel without merge conflicts.

Rules for every package: follow `CLAUDE.md` (TDD, hermetic default tests, vendor-agnostic schemas, third-party libraries wrapped behind a seam). Live-network tests go under `tests/integration/live/` behind `@pytest.mark.live`.

## Phase 1: Trust the numbers

| WP | Scope | Owns |
|----|-------|------|
| 1.1 CI | GitHub Actions: `uv sync --locked`, ruff check, ruff format check, pytest. | `.github/workflows/` |
| 1.2 Calendar-aware annualization | Periods per year depend on the universe's asset classes (crypto trades 24/7, equities ~252 sessions of 6.5h). Engine resolves asset classes from `instruments`. | `backtest/`, `core/interval.py` |
| 1.3 Unique client ids per strategy instance | Two instances of one strategy class in a backtest must not collide on `client_id` or on the engine's per-strategy pick map. | `backtest/engine.py` |
| 1.4 Out-of-sample MCPT | Permutation test scores on the validation window (optionally re-tuning per permutation) so tuning on the same data can't bias p toward passing. | `lab/survival/permutation.py` |
| 1.5 Perturbation sees all data | The perturbed in-memory lake copies every table a strategy may read (statements, metadata, instruments), not just `bars`. | `lab/survival/perturbation.py` |
| 1.6 Upgrade path for old lakes | Document that upgrading a lake through migration 005 needs the destructive opt-in and why. | `README.md` |

## Phase 2: Paper trading with a real broker

| WP | Scope | Owns |
|----|-------|------|
| 2.1 Alpaca paper broker | `AlpacaBroker` implementing the `Broker` protocol, wrapping `alpaca-py`. Order submit with `client_order_id`, status polling, fill retrieval. Config under `[brokers.alpaca]`, keys from env. | `execution/brokers/`, `config.py` (broker section) |
| 2.2 Reconciliation | Sync broker order status and fills into `orders` / `fills`, idempotently. | `execution/reconcile.py` |
| 2.3 Portfolio construction and risk | A risk layer between `strategy.decide` and the broker: position sizing, max positions, per-ticker and per-asset-class caps, cash buffer. Configurable under `[production.risk]`. | `production/risk.py`, `production/tick.py` |
| 2.4 Shadow mode | Shadow strategies are ranked too. Their hypothetical orders are logged to a `shadow_decisions` table for promotion evidence. | `production/`, `store/migrations_sqlite/` |
| 2.5 Alerting, health and P&L | Notifier seam (log + webhook). `stonks health` checks data freshness and stuck runs. `stonks pnl` reports daily P&L from snapshots. Scheduling guide for cron and Windows Task Scheduler. | `notify/`, `production/health.py`, `docs/operations.md` |
| 2.6 Broker in the tick | Tick picks its broker from config (`simulated` or `alpaca_paper`) and reconciles before deciding. | `production/tick.py`, `cli.py` |

## Phase 3: Better strategies and data

| WP | Scope | Owns |
|----|-------|------|
| 3.1 Fundamentals strategy | Point-in-time value/quality strategy on the statement tables, using `filing_date` so no statement is visible before it was filed. | `strategies/examples/`, `features/library.py`, additive read helpers in `store/lake.py` |
| 3.2 Macro regime filter | A wrapper that gates any strategy on a macro regime read from `macro_indicators`, point-in-time. | `strategies/` |
| 3.3 Walk-forward validation | Rolling train/test windows as a survival test and a runner option. | `lab/` |
| 3.4 Second data source | `YahooDataSource` wrapping `yfinance` for prices and basic profiles, same tables and column names. `--source` flag on ingest commands. | `ingest/sources/yahoo.py`, `cli.py` (ingest) |
| 3.5 Realistic costs | Per-asset-class fee and spread models and volume-aware slippage behind a `CostModel` seam used by `SimulatedBroker`. | `backtest/costs.py`, `backtest/simulated_broker.py` |

## Phase 4: Scale and operate

| WP | Scope | Owns |
|----|-------|------|
| 4.1 Reporting | `stonks report` writes a static HTML report: equity curve, drawdown, survival verdicts per strategy, live vs backtest drift. | `reporting/` |
| 4.2 Shared bar cache | Read-through, look-ahead-safe bar cache that strategies share within a backtest, replacing per-bar queries. | `strategies/_common.py`, strategies |
| 4.3 Go-live gate | `stonks golive check <id>` evaluates a paper-trading period against limits (min days, max drawdown, max live-vs-backtest drift). | `production/golive.py` |

## Phase 5: Trader interfaces

The CLI, the web UI and the MCP server all call one application service layer. No business logic lives in a transport.

| WP | Scope | Owns |
|----|-------|------|
| 5.1 Service layer and REST API | `stonks.app` services wrapping the lake, state, registry, lab, backtester and tick. FastAPI app in `stonks.api` with an OpenAPI contract, background jobs for long work (backtests, lab runs, ingest, ticks) with status polling and server-sent events, bearer-token auth for every mutating route, bound to 127.0.0.1 by default. `stonks serve` runs it and serves the built UI. | `src/stonks/app/`, `src/stonks/api/` |
| 5.2 Angular trader console | Angular (standalone components, signals) app in `web/`, typed client generated from the OpenAPI contract. Pages: dashboard (equity, P&L, drawdown, positions, health), strategies (status, survival reports, promote/retire with confirmation), lab (launch backtests and lab runs, live progress, equity curves), data (coverage, freshness, ingest runs, trigger ingest), orders and fills, shadow vs active comparison, go-live gate, alerts, settings. Keyboard-friendly, dark and light themes, accessible, and fully responsive: every page works on phones (collapsing navigation, stacked tables, touch-sized controls) as well as desktop. | `web/` |
| 5.3 MCP server | `stonks mcp` runs an MCP server on the official Python SDK over the same services. Read tools (portfolio, P&L, strategies, reports, bars, health), job tools (run backtest, lab run, ingest), and guarded write tools (promote, retire, tick) that need an explicit confirm argument and never touch a live broker. | `src/stonks/mcp/` |
| 5.5 Strategy Studio | Define, test and enable a new strategy entirely from the UI. Backend: a declarative `RuleStrategy` (indicators from `features/library.py`, entry and exit conditions, sizing, asset-class filter) stored as a validated JSON spec that round-trips through the registry. Strategy drafts table with CRUD, validate, backtest-a-draft and lab-run-a-draft jobs, and register-a-draft (lands in `shadow`). Optional Python code strategies saved under `data/user_strategies/`, off by default behind `api.allow_code_strategies`, loaded only after a subclass and smoke check. UI: visual rule builder with a live JSON view, code editor when enabled, one-click backtest with equity curve, trades and metrics, lab run with survival verdicts, then register, watch it in shadow, and enable or disable it with a toggle. | `src/stonks/strategies/rule_based.py`, `src/stonks/app/studio.py`, `src/stonks/api/routers/studio.py`, `web/` studio feature |
| 5.4 End-to-end tests and packaging | Playwright smoke tests of the main UI flows against a seeded API, and a CI job that builds and tests the UI. | `web/e2e/`, `.github/workflows/` |

## Phase 6: Strategy library from neurotrader888

Ports of the public research in github.com/neurotrader888. The broker is long-only, so every short leg becomes flat. Each module docstring credits its source repo and license and lists deviations. IntramarketDifference has no license and is rebuilt from its algorithm description only.

| WP | Scope | Owns |
|----|-------|------|
| 6.1 Research | All 14 repos triaged: 13 MIT, 1 unlicensed. Donchian, trendline breakout, RSI-PCA, permutation entropy, MCPT and the runs test were already ported. | none |
| 6.2 Shared ATR helper | True range and ATR (Wilder and simple). | `features/indicators.py` |
| 6.3 Indicator strategies | Volatility Hawkes, visibility graph path, VSA, market profile support/resistance, intramarket difference, MA crossover. | `features/{visibility_graph,vsa,market_profile}.py`, `strategies/examples/` |
| 6.4 Chart pattern strategies | Head and shoulders, flags and pennants, harmonic XABCD, market structure breaks, on confirmed extremes only. | `features/extremes.py`, `strategies/examples/` |
| 6.5 Regime filters and ML | Entropy, reversibility and runs regime filter, last-trade filter, PIP pattern miner, trendline meta-label with a random forest behind a classifier seam. | `features/{complexity,ml}.py`, `strategies/` |
| 6.6 Follow-ups | RSI-PCA holding period, trade-level runs test, walk-forward permutation test. | `strategies/examples/rsi_pca.py`, `lab/survival/` |
| 6.7 TVL indicator | Deferred. Needs a DeFi TVL data source and a `defi_tvl` table. | `ingest/`, `store/` |

## Phase 7: Learn from the books

About 60 trading books from three reading lists and the Axon "100 books" series, studied from legal sources only (open-access editions, author sites and papers, companion code). The goal is to change how Stonks thinks, not just add features.

| WP | Scope |
|----|-------|
| 7.1 Systems, sizing and risk | Chan, Carver, Davey, Kaufman, Narang, Vince, Clenow, Grimes. |
| 7.2 Markets, fundamentals and portfolios | Malkiel, Bogle, Harris, Graham, Lynch, Gray, Grinold and Kahn, Tulchinsky, Boyd, market history classics. |
| 7.3 Statistics, time series and ML | Lopez de Prado, Jansen, Dixon, Tsay, Hamilton, Ruppert, Ehlers, Stefanica, Shreve. |
| 7.4 Execution, volatility and engineering | Kissell, Johnson, Hull, Natenberg, Sinclair, Gatheral, Wilmott, Hilpisch, Strimpel, Scarpino, McKinney, Slatkin. |
| 7.5 Axon 100-book series | All seven parts, deduplicated, new titles triaged. |
| 7.6 Synthesis | `docs/principles.md` and a ranked improvement backlog. |
| 7.7 Build the best ideas | The top of the backlog, in waves, test-first. |

## Phase 8: Integration and cleanup

| WP | Scope | Owns |
|----|-------|------|
| 8.1 One strategy catalog | Connect the Strategy Studio to the catalog and services, and share one catalog between `stonks lab run` and the API. | `app/catalog.py`, `lab/catalog.py`, `app/services.py` |
| 8.2 Costs everywhere | Use the `[backtest.costs]` model in the tick and in API lab runs, not only in CLI lab runs. | `production/`, `app/lab.py` |
| 8.3 New strategies in the catalog | Register the neurotrader888 strategies and the regime and last-trade filters so the lab, API, MCP and UI can use them. | catalog |
| 8.4 Docs refresh | Bring `CLAUDE.md`, `docs/operations.md` and the block docs up to date with everything that landed. | docs |
| 8.5 Alpaca submit crash window | A crash between an Alpaca submit and writing its pending row leaves that order unrecorded. Record the order as pending before submitting and reconcile unknown broker orders by client id. | `production/tick.py`, `execution/` |
| 8.6 Splits and dividends in backtests | High priority, found by the book research. The engine and strategies trade on raw `close`, and the stored splits and dividends are never read, so a 10:1 split looks like a 90% loss and dividends are never credited. Use adjusted prices for signals, apply split ratios to holdings, and credit dividends as cash. | `backtest/`, `strategies/_common.py` |

## Phase 9: Book-driven improvements

This phase is roadmap item 7.7. It builds the backlog in `docs/research/book-lessons.md` (items BL-01 to BL-49) against the rules in `docs/principles.md`.

Items 8.2, 8.5, 8.6, 6.6, 6.7 and 5.2 are already in progress and are not repeated here. BL-01 is 8.6, and BL-07 extends 6.6's `lab/parallel.py`.

Each wave has up to six packages with disjoint file ownership. Each package also owns the test files for its own modules. The shared files are `config.py`, `config/default.toml`, `cli.py`, the API routers, the MCP server, `lab/catalog.py`, `pyproject.toml`, `tests/conftest.py` and the docs. They are only changed in the integration step after each wave.

### Wave 1: foundations

| WP | Scope | Owns |
|----|-------|------|
| 9.1.1 Trade ledger and metrics (BL-02, BL-03) | FIFO round trips and trade stats on `BacktestReport`; Sharpe with ddof=1, Sortino, Calmar, drawdown duration, skew, kurtosis, ES, turnover, costs paid. | `backtest/trades.py`, `backtest/metrics.py`, `backtest/report.py`, `backtest/simulated_broker.py`, `lab/backtesting.py` |
| 9.1.2 Trial ledger and stats (BL-04, BL-05, BL-06) | Every trial recorded with hypothesis and premortem, plus a running count per strategy class; `stonks.stats` with PSR, MinTRL, DSR, bootstrap, HAC, CSCV and FDR; reproducibility manifest. | `lab/runner.py`, `lab/trials.py`, `lab/manifest.py`, `src/stonks/stats/*`, `registry/artifact.py`, `store/migrations_sqlite/006_lab_trials.sql` |
| 9.1.3 Parallel lab (BL-07) | 6.6's pool extended to tuners: read-only snapshot lakes, one DuckDB connection per worker, spawned seeds per task, `TrialOutcome` with per-bar returns. | `lab/parallel.py`, `lab/tuning/*`, `lab/objectives.py`, `core/protocols.py`, `store/lake.py` |
| 9.1.4 Portfolio construction seam (BL-08, BL-09) | `PortfolioConstructor` ABC and registry; signal normalisation; single-winner, equal-weight, inverse-vol and vol-target constructors; buffered `orders_from_targets`; volatility estimators; cross-sectional helpers. | `src/stonks/portfolio/*`, `features/volatility.py`, `features/cross_section.py` |
| 9.1.5 Registries (BL-10, BL-11) | Survival-test registry with `quick` and `promotion` presets; `RiskRule` registry, `RiskContext`, and the existing caps as rules. | `lab/survival/registry.py`, `app/lab.py`, `production/risk.py`, `production/prices.py`, `production/rules/{__init__,caps}.py` |
| 9.1.6 Governance and metadata (BL-24, BL-26) | Status-change audit; promotion needs a passing go-live or an override with a reason; strategy hypothesis, family, label horizon and required history. | `store/migrations_sqlite/007_status_changes.sql`, `registry/store.py`, `app/strategies.py`, `strategies/base.py` |

Integration 1: realistic costs by default (BL-13), `[lab.parallel]`, the CLI and API flags, `scipy` as an explicit dependency.

### Wave 2: validation gates and construction wiring

| WP | Scope | Owns |
|----|-------|------|
| 9.2.1 Multi-strategy construction (BL-12) | One pipeline for the tick and the backtest replaces winner-take-all; per-strategy attribution; post-tick hook registry. | `production/tick.py`, `production/ranker.py`, `production/shadow.py`, `production/hooks.py`, `portfolio/pipeline.py`, `backtest/engine.py`, `store/migrations_sqlite/008_position_attribution.sql` |
| 9.2.2 Selection-bias gates (BL-14, BL-15, BL-16) | Deflated Sharpe, PBO via CSCV, a PSR-based OOS gate with a 20-trade minimum. | `lab/survival/deflated_sharpe.py`, `lab/survival/pbo.py`, `lab/survival/oos.py` |
| 9.2.3 Trade and cost robustness (BL-17, BL-18, BL-19) | Monte Carlo over trades; costs at 2× plus Carver's speed limit; parameter plateau; cross-instrument consistency. | `lab/survival/{mc_trades,cost_stress,plateau,cross_instrument}.py` |
| 9.2.4 Walk-forward and windows (BL-20, BL-21) | Embargo, walk-forward efficiency, stitched PSR and parallel folds; validation windows for perturbation, runs and period stability; MCPT n=200. | `lab/dataset.py`, `lab/survival/{walk_forward,perturbation,runs_test,period_stability,permutation}.py` |
| 9.2.5 Benchmarks (BL-22, BL-23) | Benchmark curve and stats, beta and alpha attribution, `benchmark_relative` test, tear-sheet report. | `backtest/benchmark.py`, `lab/backtesting.py`, `lab/survival/benchmark_relative.py`, `reporting/*` |
| 9.2.6 Incubation go-live (BL-25) | At least 63 days or MinTRL, whichever is longer, and 20 trades; live results inside the Monte Carlo band; a promotion checklist. | `production/golive.py` |

### Wave 3: risk, execution realism, research tools, data

| WP | Scope | Owns |
|----|-------|------|
| 9.3.1 Risk rules (BL-27) | Per-position risk budget, portfolio volatility cap, drawdown scaling, liquidity, sector cap, maximum holding time. | `production/rules/{risk_per_position,portfolio_vol,drawdown_scaling,liquidity,sector_cap,max_holding}.py` |
| 9.3.2 Circuit breaker and quit rule (BL-28, BL-29) | Monthly and weekly loss halts, a latched drawdown halt, an operational halt, a logged reset; the quit rule as a post-tick hook. | `production/rules/{circuit_breaker,operational_halt}.py`, `production/halts.py`, `production/quit_rule.py`, `production/health.py`, `store/migrations_sqlite/009_risk_halts.sql` |
| 9.3.3 Execution realism (BL-30, BL-31) | Participation cap and partial fills, limit and stop fills from the bar range, gap guard, volatility-aware impact, per-ticker spreads. | `backtest/fills.py`, `backtest/simulated_broker.py`, `backtest/engine.py`, `backtest/costs.py`, `features/spread.py` |
| 9.3.4 TCA and journal (BL-32) | Decision price and context on every order, implementation shortfall, `stonks tca` and journal services. | `core/types.py`, `store/migrations_sqlite/010_tca.sql`, `production/tca.py`, `production/tick.py`, `execution/reconcile.py` |
| 9.3.5 Signal research (BL-33, BL-34, BL-35) | Signal IC analysis, event study against baseline drift, vs-random test. | `lab/signal_eval.py`, `lab/survival/event_study.py`, `lab/survival/vs_random.py` |
| 9.3.6 Data integrity (BL-36, BL-37) | Statement audit, point-in-time universe membership, lab preflight. | `store/audit.py`, `store/migrations_duckdb/{011_statement_flags,012_universe_membership}.sql`, `store/lake.py`, `lab/universe.py`, `lab/preflight.py`, `lab/runner.py` |

### Wave 4: strategies

| WP | Scope | Owns |
|----|-------|------|
| 9.4.1 QuantMomentum (BL-38) | 12-2 momentum, top decile, frog-in-the-pan filter, quarterly rebalance. | `strategies/examples/quant_momentum.py`, `features/momentum.py` |
| 9.4.2 Stocks on the Move (BL-39) | Regression slope × R² score, moving-average and gap filters, `atr_parity` constructor. | `strategies/examples/stocks_on_the_move.py`, `features/trend.py`, `portfolio/atr_parity.py` |
| 9.4.3 Cross-asset trend (BL-40) | EWMAC, time-series momentum, all-time-high trend, `TrailingStopWrapper`. | `strategies/examples/{ewmac_trend,time_series_momentum,ath_trend}.py`, `strategies/trailing_stop.py`, `features/trend_following.py` |
| 9.4.4 QuantValue (BL-41) | EBIT/TEV value with forensic screens (STA, SNOA, Beneish, Altman) and FP/FS quality; Piotroski and magic-formula modes. | `strategies/examples/quant_value.py`, `features/fundamentals.py`, `store/lake.py` |
| 9.4.5 Composite regime filter (BL-42) | k-of-n `RegimeFilter` over macro, price trend, realised volatility, yield curve and higher-timeframe conditions. | `strategies/regime.py`, `features/regime_conditions.py` |
| 9.4.6 Legacy defaults (BL-43) | Momentum 12-1 default, breakouts moved off single stocks, vol-scaled DSL stops, metadata backfill. | existing `strategies/examples/*.py`, `strategies/rules/*` |

### Wave 5: advanced

| WP | Scope | Owns |
|----|-------|------|
| 9.5.1 Optimising constructors (BL-44) | Covariance estimators, HRP, ERC, mean-variance with costs (cvxpy, wrapped), effective number of bets. | `portfolio/{covariance,hrp,erc,optimizers,diversification}.py` |
| 9.5.2 ML hygiene (BL-45) | Purged and combinatorial CV, CPCV test, triple-barrier and uniqueness toolkit, bet sizing. | `lab/cv.py`, `lab/survival/cpcv.py`, `features/labels.py`, `features/ml.py`, `strategies/examples/trendline_meta_label.py`, `lab/dataset.py` |
| 9.5.3 Latent regimes (BL-46) | Markov-switching regime filter; VIX term-structure condition. | `features/regimes.py`, `strategies/latent_regime.py`, `features/regime_conditions_vix.py`, `ingest/sources/yahoo.py` |
| 9.5.4 Live monitoring (BL-47) | VaR/ES with violation ratio, alpha-decay monitor, correlation-to-pool test. | `production/risk_metrics.py`, `production/decay.py`, `lab/survival/pool_correlation.py`, `store/migrations_sqlite/011_risk_snapshots.sql` |
| 9.5.5 Stress (BL-48) | Crisis windows, stress simulation, `VolForecaster` with GARCH (arch, wrapped). | `lab/survival/crisis.py`, `lab/survival/stress.py`, `features/vol_forecast.py` |
| 9.5.6 Engineering guards (BL-49) | Point-in-time lake proxy, universe membership in engine and ranker, pyright, Hypothesis property tests, vectorised pre-screen. | `store/pit.py`, `lab/vectorized.py`, `pyrightconfig.json`, `backtest/engine.py`, `production/ranker.py`, `.github/workflows/ci.yml`, `tests/property/*` |

## Phase 11: Console and platform follow-ups

Found while building the console and merging Waves 2 and 3.

| WP | Scope | Owns |
|----|-------|------|
| 11.1 Go-live API route | `GET /api/strategies/{id}/golive` returning each check (name, passed, value, limit) so the Go-live page shows real results. | `app/`, `api/`, `web/` go-live page |
| 11.2 Order rejection reasons | Expose `orders.status_reason` on `OrderView` and type `exit_strategy_id` and `stale_buys_dropped` on the tick summary. | `app/`, `api/`, `web/` orders page |
| 11.3 Richer backtest results | Trade count and a drawdown series in `BacktestResult` (builds on the trade ledger, 9.1). | `app/lab.py`, `backtest/report.py` |
| 11.4 Register only if tests pass | A lab-run option that registers the strategy only when the survival suite passes, used by the Lab page, the Studio and MCP. | `app/lab.py`, `app/studio.py`, `web/`, `mcp/` |
| 11.5 Token check in Settings | Settings calls `GET /api/auth/check` instead of the stream-token probe. | `web/` settings page |
| 11.6 Smaller API and UI gaps | Lab run cost-model option, multi-kind job filter, strategy list search, health thresholds in the report, Lab and Go-live pages read `?strategy=`, 44px sort headers on phones. | `app/`, `api/`, `web/` |
| 11.7 Production polish | `[production] dividend_withholding_rate` in config; PIP miner and trendline meta-label reuse their bar cache when training; the risk cash buffer accounts for the cost model. | `config.py`, `production/`, `strategies/` |
| 11.8 Docs refresh | Bring `CLAUDE.md`, `docs/architecture.md`, `docs/operations.md`, the block docs and the wiki in line with everything that landed (was 8.4). | docs, wiki |

## Phase 12: Production readiness

What it takes to run Stonks unattended every day and trust it.

| WP | Scope |
|----|-------|
| 12.1 Market calendars | Exchange holidays and trading sessions (wrap a maintained library such as `exchange_calendars`); ticks and ingests skip closed days, crypto stays 24/7. |
| 12.2 Built-in scheduler | A scheduler seam (e.g. APScheduler) that runs ingest, tick, health and reports on the calendar, catches up missed runs, and shows next run times. Replaces hand-written cron entries. |
| 12.3 Dead-man's switch and observability | Alert when a scheduled tick or ingest did not run by its deadline; a Prometheus metrics endpoint (tick duration, orders, rejections, data age, job queue); readiness and liveness endpoints. |
| 12.4 Backups and restore | Scheduled backups of the lake and state with retention, `stonks backup` and `stonks restore`, and a tested restore drill. |
| 12.5 Data quality and fallback | Validate bars on ingest (spikes, gaps, stale, zero volume), quarantine bad rows, and fall back to a second source when the primary fails. |
| 12.6 Global kill switch | One action that halts all new orders (CLI, API, UI, MCP), audited, with a typed confirmation to resume. Pairs with the circuit breaker (Phase 9 Wave 3). |
| 12.7 Packaging and deployment | Docker image and compose file (API, UI, scheduler), a production `stonks serve` mode, Windows service instructions, environment profiles (dev, paper, live). |
| 12.8 Security hardening | Dependabot, pip-audit and npm audit in CI, CodeQL, secret scanning with push protection, HTTPS behind a reverse proxy when not on loopback, API rate limits. |
| 12.9 Releases | Semantic versions, tags, a changelog generated from commits, and a GitHub release per version. |
| 12.10 Paper soak test | Run the full daily loop on the simulated broker for weeks of historical days in fast-forward, checking idempotency, crashes mid-tick, and reconciliation every day. |
| 12.11 Runbooks | Short incident guides: tick failed, data stale, broker unreachable, disk full, restore from backup. |
| 12.12 Repo hygiene | LICENSE as all rights reserved (decided), SECURITY.md, CONTRIBUTING.md and a trading-risk disclaimer for the public repo. |

## Phase 13: Trader-ready UX

What a trader needs to use the console daily without the CLI.

| WP | Scope |
|----|-------|
| 13.1 Login and roles | Several traders will use it (decided): accounts, sessions, roles (viewer, trader, admin), every action attributed to a person in the audit trail, and a second factor before any real-money action. Replaces pasting a token. |
| 13.2 First-run wizard | Guided setup: data source key, universe, first ingest, pick a template strategy, backtest it, start paper trading. Helpful empty states everywhere. |
| 13.3 Live updates and notifications | Portfolio, ticks, jobs and alerts update live (server-sent events); a notification center; installable PWA with push notifications on phones. |
| 13.4 Universe and watchlist manager | Create and edit universes and watchlists in the UI instead of config files. |
| 13.5 Trading charts | Candlesticks with indicator overlays, trade entry and exit markers from the ledger, zoom and compare, rolling Sharpe and drawdown charts. |
| 13.6 Strategy tear sheets and leaderboard | One page per strategy (live, shadow, backtest, benchmark, trades, survival verdicts), a comparison view, and a viewer for `stonks lab sweep` results. |
| 13.7 Portfolio analytics | Exposure by asset class and sector, risk contributions, P&L attribution per strategy, monthly returns heatmap. |
| 13.8 Controls in the UI | Edit risk policy and non-secret settings with validation, the kill switch, circuit-breaker status, and the schedule. |
| 13.9 Journal | Notes on trades and strategies next to the audit trail of promotions and overrides. |
| 13.10 Command palette and shortcuts | Ctrl+K search across strategies, tickers, jobs and pages; keyboard shortcuts for common actions. |
| 13.11 In-app help | Plain-English tooltips for every metric (Sharpe, deflated Sharpe, drawdown), linked to the wiki glossary. |
| 13.12 Exports | CSV of trades, fills and P&L; PDF tear sheets; a tax-lot report from the trade ledger. |
| 13.13 Accessibility, locale and polish | WCAG 2.2 AA audit, locale-aware numbers, currency and dates, timezone preference, a Lighthouse performance budget, and a usability pass with real tasks. |
| 13.14 End-to-end tests | Playwright flows for the main trader journeys on desktop and phone sizes, in CI (was 5.4). |

## Phase 14: Hosting and maintenance

Decided: one small always-on cloud VM (for example Hetzner Cloud or DigitalOcean). Heavy lab runs stay on the owner's 32-core PC or a temporary bigger VM.

| WP | Scope |
|----|-------|
| 14.1 Infrastructure as code | Provision the VM with a script (Terraform or cloud-init): Linux, Docker, firewall closed except SSH, automatic security updates. Rebuilding the server from scratch takes one command. |
| 14.2 Compose stack | Docker Compose with the API and console, the scheduler worker, and Caddy for automatic HTTPS. Data (lake, Parquet bars, state, artifacts) lives on one mounted volume. |
| 14.3 Private access | Tailscale or Cloudflare Tunnel so traders reach the console without open ports; public exposure only by choice. |
| 14.4 Deploy pipeline | On a release tag CI builds and pushes images to GitHub Container Registry, then deploys to the VM over SSH with a health check and one-command rollback to the previous image. |
| 14.5 Backups off the server | Nightly encrypted backups (restic) of the data volume to object storage (Backblaze B2 or Cloudflare R2), retention policy, and a monthly automated restore test. |
| 14.6 Monitoring and alerting | External uptime check, dead-man pings from the scheduler (healthchecks.io or Uptime Kuma), disk, memory and CPU alerts, log retention, all routed to the existing webhook alerts. |
| 14.7 Secrets management | Secrets only in the VM's environment (or a secrets file encrypted with sops), rotated on a schedule; never in images or the repo. |
| 14.8 Maintenance routine | Dependabot or Renovate for Python, npm, Docker and GitHub Actions updates with CI gating; a monthly patch window; database migrations run automatically on deploy with a backup first. |
| 14.9 Lab offload | Run heavy lab jobs on the 32-core PC or an on-demand large VM against a read-only copy of the Parquet bars, then send results back to the server's registry. |
| 14.10 Cost and capacity | A sizing guide (CPU, RAM, disk for the lake), monthly cost estimate, and alerts before the disk fills. |

## Phase 15: Accounts, connected brokers and automation modes

Every trader gets a simple experience: connect a broker for insights, pick strategies, and choose whether Stonks acts or only notifies.

| WP | Scope |
|----|-------|
| 15.1 Design: tenancy and modes | A design doc (`docs/design/accounts-and-modes.md`) for per-user portfolios, subscriptions, broker connections and modes, and how every table, API route and job gets a user scope. Guides all later work. |
| 15.2 Per-user data model | Users, portfolios, strategy subscriptions and notification settings; every order, fill, snapshot, alert and audit row belongs to a user or portfolio, while market data and the strategy catalog stay global. Migrations with a default owner and portfolio for existing data (a golden test keeps the single-owner tick identical). Then app services take a principal, per-user API and MCP tokens with scopes, and a tenant-isolation test over every route. |
| 15.3 Broker connection seam | A `BrokerConnection` interface: read-only sync of positions, cash, orders and history first, trading later. Providers: a direct Alpaca adapter and an aggregator (for example SnapTrade) for many brokers, wrapped behind the seam. OAuth or API keys stored encrypted per user. No provider is enabled by default; an admin enables each one. |
| 15.4 Portfolio insights | Imported holdings analyzed like Stonks portfolios: allocation, exposure, P&L, risk, and which Stonks strategies agree or disagree with each holding. |
| 15.5 Automation modes | Strategies compute signals once per tick through per-strategy model books (shadow books generalised); then per subscription: notify (signals only), paper (simulated account) or auto (connected account). Auto requires a checklist and a second factor to enable, pauses itself on broker errors, and respects per-user risk limits and the kill switch at global, user and portfolio scope. |
| 15.6 Signals and notifications | A signal feed (opportunity, entry, exit, risk alerts) with reasons; Web Push through the browser's service worker so Chrome and installed phone apps get background notifications; quiet hours and per-strategy preferences. |
| 15.7 Simple trader UX | A home screen with three things: my portfolio, today's signals, my strategies with an on/off and mode switch. Advanced pages stay available but out of the way. |

## Phase 16: Short selling

| WP | Scope |
|----|-------|
| 16.1 Engine and broker | Negative positions in the portfolio, an order position effect (open or close, split at zero), short fills in the simulated broker, borrow costs, margin requirements, and short-sale availability checks. Opt-in per strategy and per portfolio; long-only behaviour stays identical by default. |
| 16.2 Risk rules for shorts | Gross and net exposure limits, per-position short caps, and squeeze protection (stop on adverse moves), as registered risk rules. |
| 16.3 Strategies that short | Let strategies emit short signals behind an opt-in; re-enable the short legs of the neurotrader888 ports and the long/short books from the book research. |
| 16.4 Validation for shorts | Backtests, permutation tests and reports handle long/short books; borrow-cost stress tests. |

## Phase 17: Options

| WP | Scope |
|----|-------|
| 17.1 Instruments and data | An option contract model (underlying, expiry, strike, right, multiplier) and an options chain data source behind the `DataSource` seam, with daily chain snapshots in the lake. Stage 1 with 17.2 is read-only analytics; nothing trades options until 17.3 to 17.5. |
| 17.2 Pricing and Greeks | Black-Scholes and implied volatility through a maintained library (for example py_vollib or QuantLib), wrapped behind a seam; volatility surface basics. |
| 17.3 Backtesting options | Fills on option prices, expiry and assignment handling, early exercise rules, and multi-leg positions. |
| 17.4 Risk for options | Greek limits (delta, gamma, vega), max loss per spread, and margin. |
| 17.5 Options strategies | Covered calls, cash-secured puts, protective puts, vertical spreads, and volatility strategies from the book research (Sinclair, Natenberg). |

## Execution order

1. Wave 1 in parallel: backtest (1.2, 1.3, 3.5), lab (1.4, 1.5, 3.3), production (2.3, 2.4, 2.5), broker (2.1, 2.2), data (3.4), strategies (3.1, 3.2, 4.2), and the service layer plus REST API for existing features (5.1).
2. Merge, review the combined diff, fix confirmed findings.
3. Wave 2: broker in the tick (2.6), reporting (4.1), go-live gate (4.3), API routes for the Wave 1 features, MCP server (5.3), Strategy Studio backend (5.5).
4. In parallel with Wave 2: neurotrader888 ports (6.1 to 6.5) and book research (7.1 to 7.5).
5. Integration and cleanup (8.1 to 8.6), the port follow-ups (6.6, 6.7), and the book synthesis (7.6).
6. Angular trader console (5.2) including the Strategy Studio UI (5.5), then end-to-end tests (5.4). This runs alongside Phase 9, which never touches `web/`.
7. Phase 9 (7.7), once 8.2, 8.5, 8.6, 6.6 and 6.7 are merged. Waves 9.1 to 9.5 run in order, up to six agents per wave, each wave followed by its integration step (merge, wire the shared files, review, fix, full test run).
8. Final review, fix, re-review.
9. Phases 15 to 17 follow the design docs in `docs/design/` (`accounts-and-modes.md`, `shorting.md`, `options.md`). The first step of 15.2 (accounts data model, no behaviour change) lands **before W2.1 (9.2.1) is wired into the tick**, so W2.1 writes the tick once as a loop over portfolios with a `BookSpec` instead of rewriting it twice. The backend of 13.1 (login, roles, 2FA, tokens) runs as part of Phase 15; see the step plan in `accounts-and-modes.md` section 12.
