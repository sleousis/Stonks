# Roadmap

This roadmap took Stonks from a research engine with a simulated loop to paper trading, better strategies and unattended operation, and lists what comes next. Each work package (WP) lists the files it owns so packages can be built in parallel without merge conflicts.

## Status (September 2026)

| Phase | Status |
|-------|--------|
| 1 to 8 | Done, except 5.4 end-to-end tests (now 13.14). 8.4 moved to 11.8. |
| 9 | Waves 1 to 5 done. Details under Phase 9. |
| 10 | Done: 10.1 to 10.5. |
| 11 | Done except parts of 11.6. 11.8 is this docs refresh. |
| 12 | Mostly done. Open: three runbooks (tick failed, broker unreachable, disk full). |
| 13 | Partly done: PWA and push, command palette, in-app help, accessibility and locale. The rest is planned. |
| 14 | Done. |
| 15 | Mostly done: design, data model, connection seam, insights, automation modes, notifications, home screen. The tick trades one book per portfolio. Open: order placement for real providers. |
| 16 | 16.1 and 16.2 done, off by default. 16.3 and 16.4 planned. |
| 17 | Planned. |
| 19 | Wave 1 done: 19.1, 19.4, 19.6 and 19.7, with console screens. 19.2 IBKR adapter, 19.3 connection and borrow, 19.5 reconciliation and drift, 19.8 tickets and approve mode, and 19.16 broker edge cases done. Design: `docs/design/live-trading.md`. |
| 20 | 20.1 to 20.8 done, backend and console. |
| 21 | 21.1 (streaming data) done, off by default. 21.2 and 21.3 planned in small work packages. Design: `docs/design/intraday.md`. |
| 22 | All of 22.1 to 22.9 done. Factors: `docs/factors.md`. |

Rules for every package: follow `CLAUDE.md` (TDD, hermetic default tests, vendor-agnostic schemas, third-party libraries wrapped behind a seam). Live-network tests go under `tests/integration/live/` behind `@pytest.mark.live`.

## Phase 1: Trust the numbers

**Status:** done.

| WP | Scope | Owns |
|----|-------|------|
| 1.1 CI | GitHub Actions: `uv sync --locked`, ruff check, ruff format check, pytest. | `.github/workflows/` |
| 1.2 Calendar-aware annualization | Periods per year depend on the universe's asset classes (crypto trades 24/7, equities ~252 sessions of 6.5h). Engine resolves asset classes from `instruments`. | `backtest/`, `core/interval.py` |
| 1.3 Unique client ids per strategy instance | Two instances of one strategy class in a backtest must not collide on `client_id` or on the engine's per-strategy pick map. | `backtest/engine.py` |
| 1.4 Out-of-sample MCPT | Permutation test scores on the validation window (optionally re-tuning per permutation) so tuning on the same data can't bias p toward passing. | `lab/survival/permutation.py` |
| 1.5 Perturbation sees all data | The perturbed in-memory lake copies every table a strategy may read (statements, metadata, instruments), not just `bars`. | `lab/survival/perturbation.py` |
| 1.6 Upgrade path for old lakes | Document that upgrading a lake through migration 005 needs the destructive opt-in and why. | `README.md` |

## Phase 2: Paper trading with a real broker

**Status:** done. Alpaca is opt-in through `[brokers].kind`; the tick writes each order as pending before submitting it.

| WP | Scope | Owns |
|----|-------|------|
| 2.1 Alpaca paper broker | `AlpacaBroker` implementing the `Broker` protocol, wrapping `alpaca-py`. Order submit with `client_order_id`, status polling, fill retrieval. Config under `[brokers.alpaca]`, keys from env. | `execution/brokers/`, `config.py` (broker section) |
| 2.2 Reconciliation | Sync broker order status and fills into `orders` / `fills`, idempotently. | `execution/reconcile.py` |
| 2.3 Portfolio construction and risk | A risk layer between `strategy.decide` and the broker: position sizing, max positions, per-ticker and per-asset-class caps, cash buffer. Configurable under `[production.risk]`. | `production/risk.py`, `production/tick.py` |
| 2.4 Shadow mode | Shadow strategies are ranked too. Their hypothetical orders are logged to a `shadow_decisions` table for promotion evidence. | `production/`, `store/migrations_sqlite/` |
| 2.5 Alerting, health and P&L | Notifier seam (log + webhook). `stonks health` checks data freshness and stuck runs. `stonks pnl` reports daily P&L from snapshots. Scheduling guide for cron and Windows Task Scheduler. | `notify/`, `production/health.py`, `docs/operations.md` |
| 2.6 Broker in the tick | Tick picks its broker from config (`simulated` or `alpaca_paper`) and reconciles before deciding. | `production/tick.py`, `cli.py` |

## Phase 3: Better strategies and data

**Status:** done (`quality_value`, `macro_regime_filter`, `walk_forward`, Yahoo source, `[backtest.costs]`).

| WP | Scope | Owns |
|----|-------|------|
| 3.1 Fundamentals strategy | Point-in-time value/quality strategy on the statement tables, using `filing_date` so no statement is visible before it was filed. | `strategies/examples/`, `features/library.py`, additive read helpers in `store/lake.py` |
| 3.2 Macro regime filter | A wrapper that gates any strategy on a macro regime read from `macro_indicators`, point-in-time. | `strategies/` |
| 3.3 Walk-forward validation | Rolling train/test windows as a survival test and a runner option. | `lab/` |
| 3.4 Second data source | `YahooDataSource` wrapping `yfinance` for prices and basic profiles, same tables and column names. `--source` flag on ingest commands. | `ingest/sources/yahoo.py`, `cli.py` (ingest) |
| 3.5 Realistic costs | Per-asset-class fee and spread models and volume-aware slippage behind a `CostModel` seam used by `SimulatedBroker`. | `backtest/costs.py`, `backtest/simulated_broker.py` |

## Phase 4: Scale and operate

**Status:** done (`stonks report`, the shared `BarCache`, `stonks golive check`).

| WP | Scope | Owns |
|----|-------|------|
| 4.1 Reporting | `stonks report` writes a static HTML report: equity curve, drawdown, survival verdicts per strategy, live vs backtest drift. | `reporting/` |
| 4.2 Shared bar cache | Read-through, look-ahead-safe bar cache that strategies share within a backtest, replacing per-bar queries. | `strategies/_common.py`, strategies |
| 4.3 Go-live gate | `stonks golive check <id>` evaluates a paper-trading period against limits (min days, max drawdown, max live-vs-backtest drift). | `production/golive.py` |

## Phase 5: Trader interfaces

**Status:** 5.1, 5.2, 5.3 and 5.5 done. 5.4 (Playwright end-to-end tests) is not done and continues as 13.14.

The CLI, the web UI and the MCP server all call one application service layer. No business logic lives in a transport.

| WP | Scope | Owns |
|----|-------|------|
| 5.1 Service layer and REST API | `stonks.app` services wrapping the lake, state, registry, lab, backtester and tick. FastAPI app in `stonks.api` with an OpenAPI contract, background jobs for long work (backtests, lab runs, ingest, ticks) with status polling and server-sent events, bearer-token auth for every mutating route, bound to 127.0.0.1 by default. `stonks serve` runs it and serves the built UI. | `src/stonks/app/`, `src/stonks/api/` |
| 5.2 Angular trader console | Angular (standalone components, signals) app in `web/`, typed client generated from the OpenAPI contract. Pages: dashboard (equity, P&L, drawdown, positions, health), strategies (status, survival reports, promote/retire with confirmation), lab (launch backtests and lab runs, live progress, equity curves), data (coverage, freshness, ingest runs, trigger ingest), orders and fills, shadow vs active comparison, go-live gate, alerts, settings. Keyboard-friendly, dark and light themes, accessible, and fully responsive: every page works on phones (collapsing navigation, stacked tables, touch-sized controls) as well as desktop. | `web/` |
| 5.3 MCP server | `stonks mcp` runs an MCP server on the official Python SDK over the same services. Read tools (portfolio, P&L, strategies, reports, bars, health), job tools (run backtest, lab run, ingest), and guarded write tools (promote, retire, tick) that need an explicit confirm argument and never touch a live broker. | `src/stonks/mcp/` |
| 5.5 Strategy Studio | Define, test and enable a new strategy entirely from the UI. Backend: a declarative `RuleStrategy` (indicators from `features/library.py`, entry and exit conditions, sizing, asset-class filter) stored as a validated JSON spec that round-trips through the registry. Strategy drafts table with CRUD, validate, backtest-a-draft and lab-run-a-draft jobs, and register-a-draft (lands in `shadow`). Optional Python code strategies saved under `data/user_strategies/`, off by default behind `api.allow_code_strategies`, loaded only after a subclass and smoke check. UI: visual rule builder with a live JSON view, code editor when enabled, one-click backtest with equity curve, trades and metrics, lab run with survival verdicts, then register, watch it in shadow, and enable or disable it with a toggle. | `src/stonks/strategies/rule_based.py`, `src/stonks/app/studio.py`, `src/stonks/api/routers/studio.py`, `web/` studio feature |
| 5.4 End-to-end tests and packaging | Playwright smoke tests of the main UI flows against a seeded API, and a CI job that builds and tests the UI. | `web/e2e/`, `.github/workflows/` |

## Phase 6: Strategy library from neurotrader888

**Status:** done, including 6.6 and 6.7 (`stonks ingest tvl` from DefiLlama, `defi_tvl`, `tvl_deviation`).

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

**Status:** done. The synthesis is `docs/principles.md` and the backlog `docs/research/book-lessons.md`; 7.7 is Phase 9.

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

**Status:** done. 8.4 continues as 11.8.

| WP | Scope | Owns |
|----|-------|------|
| 8.1 One strategy catalog | Connect the Strategy Studio to the catalog and services, and share one catalog between `stonks lab run` and the API. | `app/catalog.py`, `lab/catalog.py`, `app/services.py` |
| 8.2 Costs everywhere | Use the `[backtest.costs]` model in the tick and in API lab runs, not only in CLI lab runs. | `production/`, `app/lab.py` |
| 8.3 New strategies in the catalog | Register the neurotrader888 strategies and the regime and last-trade filters so the lab, API, MCP and UI can use them. | catalog |
| 8.4 Docs refresh | Bring `CLAUDE.md`, `docs/operations.md` and the block docs up to date with everything that landed. | docs |
| 8.5 Alpaca submit crash window | A crash between an Alpaca submit and writing its pending row leaves that order unrecorded. Record the order as pending before submitting and reconcile unknown broker orders by client id. | `production/tick.py`, `execution/` |
| 8.6 Splits and dividends in backtests | High priority, found by the book research. The engine and strategies trade on raw `close`, and the stored splits and dividends are never read, so a 10:1 split looks like a 90% loss and dividends are never credited. Use adjusted prices for signals, apply split ratios to holdings, and credit dividends as cash. | `backtest/`, `strategies/_common.py` |

## Phase 9: Book-driven improvements

**Status:**

- Wave 1: done.
- Wave 2: done. The tick runs its books through the shared pipeline; backtests use it only when `BacktestConfig.construction` is set, which the lab and API don't do yet.
- Wave 3: done. The rules are set under `[production.risk.rules.*]`, the circuit breaker and operational halt are off by default, and the quit rule alerts after every tick (`[production.quit_rule]`). Halts are listed and cleared with `stonks halts`. The lab runs the data preflight before tuning, and `stonks audit statements` checks the statements (also after `stonks ingest fundamentals`). 9.3.4 records the decision price and context on every order and reports implementation shortfall and the trade journal with `stonks tca`, `/api/tca` and MCP tools. Round trips with MAE and MFE in the journal are not built yet.
- Wave 4: done. 9.4.1 to 9.4.4 (`quant_momentum`, `stocks_on_the_move` with `atr_parity`, `ewmac_trend`, `tsmom`, `ath_trend`, `TrailingStopWrapper`, `quant_value`), 9.4.5 (`RegimeFilter`) and 9.4.6 (legacy defaults and metadata backfill).
- Wave 5: 9.5.1 done. The `hrp`, `erc` and `mean_variance_costs` constructors, four covariance estimators and the effective number of bets are in `portfolio/`. The tick and the backtest fill `returns_history` with daily returns of adjusted closes up to the decision, read through the point-in-time view once per decision (`portfolio/returns.py`). The lookback is the constructor's `lookback` setting. Volumes reach `mean_variance_costs`, so its impact term works. Books on `single_winner` and the other constructors load nothing new. Restated statements are versioned (DuckDB `018`), so a restatement Stonks saw after a decision stays hidden from it (P12).
- Wave 5: 9.5.4 done. After each real tick the `risk_monitor` hook writes daily VaR and ES per portfolio and per strategy sleeve to `risk_snapshots` (SQLite `019`), with the violation ratio, a Kupiec test and the alpha-decay check. Alerts go to the portfolio owner, `health` warns on a bad violation ratio, and `/api/risk/live`, `/api/risk/snapshots` and two MCP tools read them. The `pool_correlation` survival test is in the registry but in no preset yet. The monitor reads `[production.risk_monitor]` and `[production.decay]`. `registry audit` with BH is not built.
- Wave 5: 9.5.2 done. Purged and combinatorial purged k-fold and `CVObjective` (`lab/cv.py`), the `cpcv` test (in `promotion`), `LabDataset.train_segments`, the triple-barrier and uniqueness toolkit (`features/labels.py`), bet sizing and sample weights (`features/ml.py`). `trendline_meta_label` fits each CV segment separately, weights trades by uniqueness, reports a purged CV score and trades above the barriers' break-even probability. The lab objectives `cv_sharpe`, `cv_cagr` and `cv_final_return` tune on purged folds through `CVObjective` (CLI, API, MCP and the Lab page).
- Wave 5: 9.5.3 done. `MarkovSwitchingRegime` (statsmodels, wrapped, with our own Hamilton filter) in `features/regimes.py`, the `latent_regime_filter` wrapper, the `vix_term_structure` condition (`features/regime_vix.py`) and Yahoo's `vix_spot` and `vix_3m` macro series.
- Wave 5: 9.5.5 done. The `crisis` test (in `promotion`), the `stress` test (block bootstrap or GARCH-t filtered historical simulation, in no preset) and the `VolForecaster` seam (`ewma`, `garch` wrapping `arch`, `har_rv`) in `features/vol_forecast.py`.

Migration numbers in the tables below were plans. The landed ones are SQLite `008_lab_trials`, `009_status_changes`, `014_position_attribution`, `016_risk_halts` and `017_tca`, and DuckDB `014_statement_flags` and `015_universe_membership`. New migrations take the next free number.

This phase is roadmap item 7.7. It builds the backlog in `docs/research/book-lessons.md` (items BL-01 to BL-49) against the rules in `docs/principles.md`.

Items 8.2, 8.5, 8.6, 6.6, 6.7 and 5.2 were built before this phase and are not repeated here. BL-01 is 8.6, and BL-07 extends 6.6's `lab/parallel.py`.

Each wave has up to six packages with disjoint file ownership. Each package also owns the test files for its own modules. The shared files are `config.py`, `config/default.toml`, `cli.py`, the API routers, the MCP server, `lab/catalog.py`, `pyproject.toml`, `tests/conftest.py` and the docs. They are only changed in the integration step after each wave.

### Wave 1: foundations

| WP | Scope | Owns |
|----|-------|------|
| 9.1.1 Trade ledger and metrics (BL-02, BL-03) | FIFO round trips and trade stats on `BacktestReport`; Sharpe with ddof=1, Sortino, Calmar, drawdown duration, skew, kurtosis, ES, turnover, costs paid. | `backtest/trades.py`, `backtest/metrics.py`, `backtest/report.py`, `backtest/simulated_broker.py`, `lab/backtesting.py` |
| 9.1.2 Trial ledger and stats (BL-04, BL-05, BL-06) | Every trial recorded with hypothesis and premortem, plus a running count per strategy class; `stonks.stats` with PSR, MinTRL, DSR, bootstrap, HAC, CSCV and FDR; reproducibility manifest. | `lab/runner.py`, `lab/trials.py`, `lab/manifest.py`, `src/stonks/stats/*`, `registry/artifact.py`, `store/migrations_sqlite/008_lab_trials.sql` |
| 9.1.3 Parallel lab (BL-07) | 6.6's pool extended to tuners: read-only snapshot lakes, one DuckDB connection per worker, spawned seeds per task, `TrialOutcome` with per-bar returns. | `lab/parallel.py`, `lab/tuning/*`, `lab/objectives.py`, `core/protocols.py`, `store/lake.py` |
| 9.1.4 Portfolio construction seam (BL-08, BL-09) | `PortfolioConstructor` ABC and registry; signal normalisation; single-winner, equal-weight, inverse-vol and vol-target constructors; buffered `orders_from_targets`; volatility estimators; cross-sectional helpers. | `src/stonks/portfolio/*`, `features/volatility.py`, `features/cross_section.py` |
| 9.1.5 Registries (BL-10, BL-11) | Survival-test registry with `quick` and `promotion` presets; `RiskRule` registry, `RiskContext`, and the existing caps as rules. | `lab/survival/registry.py`, `app/lab.py`, `production/risk.py`, `production/prices.py`, `production/rules/{__init__,caps}.py` |
| 9.1.6 Governance and metadata (BL-24, BL-26) | Status-change audit; promotion needs a passing go-live or an override with a reason; strategy hypothesis, family, label horizon and required history. | `store/migrations_sqlite/009_status_changes.sql`, `registry/store.py`, `app/strategies.py`, `strategies/base.py` |

Integration 1: realistic costs by default (BL-13), `[lab.parallel]`, the CLI and API flags, `scipy` as an explicit dependency.

### Wave 2: validation gates and construction wiring

| WP | Scope | Owns |
|----|-------|------|
| 9.2.1 Multi-strategy construction (BL-12) | One pipeline for the tick and the backtest replaces winner-take-all; per-strategy attribution; post-tick hook registry. | `production/tick.py`, `production/ranker.py`, `production/shadow.py`, `production/hooks.py`, `portfolio/pipeline.py`, `backtest/engine.py`, `store/migrations_sqlite/014_position_attribution.sql` |
| 9.2.2 Selection-bias gates (BL-14, BL-15, BL-16) | Deflated Sharpe, PBO via CSCV, a PSR-based OOS gate with a 20-trade minimum. | `lab/survival/deflated_sharpe.py`, `lab/survival/pbo.py`, `lab/survival/oos.py` |
| 9.2.3 Trade and cost robustness (BL-17, BL-18, BL-19) | Monte Carlo over trades; costs at 2× plus Carver's speed limit; parameter plateau; cross-instrument consistency. | `lab/survival/{mc_trades,cost_stress,plateau,cross_instrument}.py` |
| 9.2.4 Walk-forward and windows (BL-20, BL-21) | Embargo, walk-forward efficiency, stitched PSR and parallel folds; validation windows for perturbation, runs and period stability; MCPT n=200. | `lab/dataset.py`, `lab/survival/{walk_forward,perturbation,runs_test,period_stability,permutation}.py` |
| 9.2.5 Benchmarks (BL-22, BL-23) | Benchmark curve and stats, beta and alpha attribution, `benchmark_relative` test, tear-sheet report. | `backtest/benchmark.py`, `lab/backtesting.py`, `lab/survival/benchmark_relative.py`, `reporting/*` |
| 9.2.6 Incubation go-live (BL-25) | At least 63 days or MinTRL, whichever is longer, and 20 trades; live results inside the Monte Carlo band; a promotion checklist. | `production/golive.py` |

### Wave 3: risk, execution realism, research tools, data

| WP | Scope | Owns |
|----|-------|------|
| 9.3.1 Risk rules (BL-27) | Per-position risk budget, portfolio volatility cap, drawdown scaling, liquidity, sector cap, maximum holding time. | `production/rules/{risk_per_position,portfolio_vol,drawdown_scaling,liquidity,sector_cap,max_holding}.py` |
| 9.3.2 Circuit breaker and quit rule (BL-28, BL-29) | Monthly and weekly loss halts, a latched drawdown halt, an operational halt and a logged reset. The quit rule runs as a post-tick hook. | `production/rules/{circuit_breaker,operational_halt}.py`, `production/halts.py`, `production/quit_rule.py`, `production/health.py`, `store/migrations_sqlite/016_risk_halts.sql` |
| 9.3.3 Execution realism (BL-30, BL-31) | Participation cap and partial fills, limit and stop fills from the bar range, gap guard, volatility-aware impact, per-ticker spreads. | `backtest/fills.py`, `backtest/simulated_broker.py`, `backtest/engine.py`, `backtest/costs.py`, `features/spread.py` |
| 9.3.4 TCA and journal (BL-32) | Decision price and context on every order, implementation shortfall, `stonks tca` and journal services. | `core/types.py`, `store/migrations_sqlite/010_tca.sql`, `production/tca.py`, `production/tick.py`, `execution/reconcile.py` |
| 9.3.5 Signal research (BL-33, BL-34, BL-35) | Signal IC analysis, event study against baseline drift, vs-random test. | `lab/signal_eval.py`, `lab/survival/event_study.py`, `lab/survival/vs_random.py` |
| 9.3.6 Data integrity (BL-36, BL-37) | Statement audit, point-in-time universe membership, lab preflight. | `store/audit.py`, `store/migrations_duckdb/{014_statement_flags,015_universe_membership}.sql`, `store/lake.py`, `lab/universe.py`, `lab/preflight.py`, `lab/runner.py` |

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
| 9.5.3 Latent regimes (BL-46) | Markov-switching regime filter; VIX term-structure condition. | `features/regimes.py`, `strategies/latent_regime.py`, `features/regime_vix.py`, `ingest/sources/yahoo.py` |
| 9.5.4 Live monitoring (BL-47) | VaR/ES with violation ratio, alpha-decay monitor, correlation-to-pool test. | `production/risk_metrics.py`, `production/decay.py`, `lab/survival/pool_correlation.py`, `store/migrations_sqlite/019_risk_snapshots.sql` |
| 9.5.5 Stress (BL-48) | Crisis windows, stress simulation, `VolForecaster` with GARCH (arch, wrapped). | `lab/survival/crisis.py`, `lab/survival/stress.py`, `features/vol_forecast.py` |
| 9.5.6 Engineering guards (BL-49) | Done. Strategies read the lake through `PointInTimeLake` (`store/pit.py`) in the engine, the ranker and the scoring workers: every read stops at the decision, raw SQL raises. A planted-future test runs every catalogued strategy (`tests/unit/test_pit_catalog.py`). The engine already honoured membership (RS-05); the ranker now skips non-members of a stored tick universe. `lab/vectorized.py`: an opt-in `PrescreenTuner` that ranks a big grid with a fast approximate backtest and runs full backtests only on the best, with a parity test against the engine for `Momentum`. Hypothesis property tests in `tests/property/` (they found a NaN fill price in the sqrt_vol and I-Star impact models, now fixed). `core/` and `execution/` pass pyright strict, and the gate refuses any baseline error under a strict path. | `store/pit.py`, `lab/vectorized.py`, `pyproject.toml` `[tool.pyright]`, `tools/pyright_gate.py`, `backtest/engine.py`, `production/ranker.py`, `tests/property/*` |

## Phase 10: Repository, docs and data scale

**Status:** 10.1 to 10.5 done.

| WP | Scope | Status | Owns |
|----|-------|--------|------|
| 10.1 Public repo and branch protection | Protect `main`: changes land as squash PRs from `feat/roadmap` with CI green. Open the repo to the public. | Done. The repo is public after a full secret scan, and `main` needs a PR with passing `test` and `ui` checks, with no force push or deletion. | GitHub settings, `.github/workflows/` |
| 10.2 GitHub wiki | Guides and the glossary on the wiki. `docs.yml` syncs the API and MCP references there. | Done. The wiki holds the guides and glossary, and `docs.yml` syncs the API and MCP references on every merge to `main`. | `.github/workflows/docs.yml`, wiki |
| 10.3 API docs from code | `docs/api/rest.md` from the OpenAPI spec, `docs/api/mcp-tools.{json,md}` from the MCP tools, each route's permission as `x-permission`, Swagger UI on Pages. Tests fail when a checked-in copy is stale. | Done. | `api/openapi.py`, `api/docs.py`, `mcp/docs.py`, `docs/api/` |
| 10.4 Parquet bar store | `BarStore` seam with the DuckDB table and hive-partitioned Parquet files that other processes can read while `stonks serve` holds the lake. `bars_migrate` moves the bars and switches. | Done. | `store/bars.py`, `store/bars_migrate.py`, `[lake.bars]` |
| 10.5 Dynamic universes and on-demand tickers | Stored universes (list, exchange, rule, index) with point-in-time membership, index history import, and `DataEnsurer` that fetches only missing bars. Wired into settings (`[ensure]`, `[production].universe` as an id), the tick, `stonks universe` and `stonks lab run --universe-id --ensure-data`, API lab runs (`universe_id`, `ensure_data`), MCP and a daily scheduled refresh. | Done. The console universes page is still open. | `universes/`, `ingest/ensure.py`, `production/universe.py`, `app/universes.py`, `docs/universes.md` |

## Phase 11: Console and platform follow-ups

**Status:** 11.1 to 11.5 and 11.7 done; 11.6 mostly done; 11.8 is this refresh.

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

**Status:**

- Done: 12.1 calendars, 12.2 scheduler (`stonks schedule`), 12.3 dead-man's switch and observability (deadlines, pings, `GET /metrics`, probes, and the backup, push delivery and sync jobs), 12.4 backups (`stonks backup`, and `POST /api/backups` while the server holds the lake), 12.5 data quality and fallback (`[ingest.quality]`, `[ingest.fallback]`), 12.6 kill switch (`stonks halts kill`, the API, the MCP `engage_kill_switch` tool, resume needs a typed confirmation), 12.7 Docker and Compose, 12.8 security checks in CI, 12.9 releases, 12.10 paper soak (`tests/soak`, a smoke run in the default suite, the long run weekly in `soak.yml`), 12.12 repo hygiene.
- Partly: 12.11 (runbooks for stale data, restore and failed deploys).

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

**Status:**

- Done: 13.1 (sign-in, roles, TOTP, step-up; see `docs/security.md`), 13.3, 13.9 (trade journal with notes next to the status history), 13.10, 13.11, 13.13, 13.14 (Playwright journeys on desktop and 375px).
- Done: 13.2 first-run wizard. `/welcome` walks a trader through five steps (second factor, portfolio, watchlist or universe, follow a strategy, push alerts). Each can be skipped; progress is stored per user (`onboarding_steps`, `onboarding_status`, migration 026) and steps the data shows done tick themselves. Today shows a "Finish setting up" card. Admins also get an install checklist: data source key, first data load, a backup on disk, a running scheduler (`/api/onboarding`, `/api/onboarding/system`).
- Done: 13.4. Universes are managed on `/universes`. Watchlists (`watchlists`, migration 026, `/api/watchlists`, never shared) live on `/watchlists`: a list opens in the lab as its tickers, and filters Today's tape and signals and the chart picker.
- Done: 13.5 trading charts. `/charts/:ticker` draws daily candles, volume, moving averages (20, 50, 200), your fills as B and S and strategy signals, from one read (`/api/charts/{ticker}`), through the `ChartEngine` seam (Lightweight Charts, lazy loaded). Ranges from 3 months to all; works on phones. Open: compare several tickers, rolling Sharpe.
- Done: 13.6. `/leaderboard` ranks strategies by risk-adjusted paper result with trade counts, survival tests and the go-live verdict; `/strategies/:id/tearsheet` gathers the paper curve, monthly returns, recent trades, survival verdicts, the go-live report and the status history. The sweep viewer is on `/lab/sweeps`. Open: a PDF tear sheet.
- Done: 13.8. Kill switch, breaker and halts (`/ops/halts`), the schedule and backups, health checks, users and settings were already in the console; a trader can now set their own risk limits in Settings (`/api/risk/limits`, tighten only, audited). Left for operators on purpose: bulk ingests, the statement audit and TCA refresh (scheduled jobs), database setup and the servers (see `tests/parity/capabilities.toml`).
- Done: 13.12 CSV exports of orders, fills, the trade journal, snapshots, daily P&L and lab trials (`/api/exports/*`), downloaded over the session from the Orders, Trade costs, Insights and trial ledger pages. Open: PDF tear sheets and a tax-lot report.
- 13.7 portfolio analytics is covered by Insights and Risk (15.4, 9.x); the monthly returns heatmap per portfolio is open.

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

**Status:** done. See `docs/deploy.md` and `docs/capacity.md`.

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
| 14.9 Lab offload | Done. A `LabExecutor` seam: in process (the default) or a lab worker (`python -m stonks.lab.offload worker`, the optional `lab-worker` Compose service with its own CPU and memory limits). The API queues lab runs, sweeps and Studio lab runs in the state DB (migration 025) and publishes a read-only lake snapshot, the worker runs them with the same handlers and writes results to the same job rows. Heartbeats, cancel, lost-worker recovery, a `lab_queue` health check and `stonks_lab_*` metrics. Open: a queue over the API, so a worker on another machine (the 32-core PC) can pull jobs. |
| 14.10 Cost and capacity | Done. `docs/capacity.md`: measured tick, lab, storage, memory and API latency numbers, VM sizes and cost ranges for 1, 5 and 20 traders, and the limits. `tools/benchmark.py` and `tools/api_load.py` reproduce them. Disk alerts come from `check-host.sh` (14.6). |

## Phase 15: Accounts, connected brokers and automation modes

**Status:**

- Done: 15.1 design; 15.2 data model (migration 010, default owner `usr_owner` and portfolio `pf_default`, scoped services, golden single-owner tick); 15.3 read-only connection seam with Alpaca, SnapTrade and fake providers (`python -m stonks.connections`); the 15.6 notification backend (outbox, Web Push, email, webhook, quiet hours, preferences).
- Done: 15.5. The tick stores every scored strategy's signals and signal events with a plain reason (migration 020) and can score opted-in strategies on all cores. Per subscription, notify sends the events to the outbox, paper trades a simulated account (a broker portfolio gets its own paper account) and auto trades the connected account. Auto needs the checklist (20 paper days from `portfolio_runs`, a healthy connection that can trade, no halt) and a fresh second factor, pauses itself on a broker error, and obeys owner risk limits and the kill switch at every scope. The entrypoints build these per-portfolio books by default (`[production] books_from_subscriptions = true`). A promotion subscribes `pf_default` to the strategy (paper, or auto at an external broker, audited as `service:system`) and migration 022 did the same for the strategies already active, so the default book trades exactly as the old single book did. A parity test checks it. Still open: no provider but the fake one can place orders yet. 15.6 delivery works and the tick now enqueues signals.
- Done: 15.4 insights. Any portfolio you own, a synced broker account too, shows allocation (asset class, sector, currency, ticker), exposure (gross, net, beta), P&L over periods, risk (volatility, drawdown, VaR, concentration) and which active strategies agree or disagree with each holding, and why. Routes under `/api/insights`, MCP tools `get_insights` and `get_strategy_agreement`. Admins see totals only.
- Done: per-user API and MCP tokens. `stonks mcp` acts as the owner of `STONKS_MCP_TOKEN`, with that token's scopes. A test calls every MCP tool as several people and checks it is refused exactly when its REST route is.
- Done: 15.7 simple home screen (portfolio, today’s signals, my strategies with the mode switch).

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

**Status:** 16.1 to 16.4 done, off by default. Design and what changed from it: `docs/design/shorting.md`. A short book's paper broker charges borrow fees and debit interest for every day since the stored accrual date (migration 023, `production/financing.py`). Still open: the margin-call notification. The lake table of borrow rates (`borrow_rates`, `LakeBorrowSource`) and broker-reported borrow (`IbkrBorrowSource`) landed with 19.3.

| WP | Scope |
|----|-------|
| 16.1 Engine and broker | Negative positions in the portfolio, an order position effect (open or close, split at zero), short fills in the simulated broker, borrow costs, margin requirements, and short-sale availability checks. Opt-in per strategy and per portfolio; long-only behaviour stays identical by default. |
| 16.2 Risk rules for shorts | Gross and net exposure limits, per-position short caps, and squeeze protection (stop on adverse moves), as registered risk rules. |
| 16.3 Strategies that short | Done. A `short_mode` param on short-capable strategies (off by default). The ranker keeps short scores for books that may short. `equal_weight_top_n` and `vol_target` have long/short modes with gross and net limits and dollar or beta neutrality. EWMAC and TSMOM short down trends, and two new strategies trade long/short: `ls_momentum` and `pairs_reversion`. The neurotrader888 short legs are not re-enabled yet. |
| 16.4 Validation for shorts | Done. The backtest report shows financing, forced orders, exposure over time and long and short P&L. The trade Monte Carlo and runs test read short round trips. `cost_stress` multiplies borrow fees and adds a 3x borrow stress, `stress` adds a short-squeeze scenario, and go-live checks that a short strategy was validated with realistic borrow costs. |

## Phase 17: Options

**Status:** 17.1 to 17.5 done as research, off by default. Nothing in the tick, the console or MCP trades options. Design and what changed from it: `docs/design/options.md`. Still open: live options through Interactive Brokers (after Phase 19), options in the console and MCP, Treasury rates and dividends in pricing, and a paid chain history for real validation.

| WP | Scope |
|----|-------|
| 17.1 Instruments and data | Done. `core/options.py` holds the contract (underlying, expiry, strike, right, multiplier, style, settlement), its canonical id, OCC symbols and the OCC split adjustment. Lake migration 017 adds `option_contracts` and `option_quotes` with the vendor's IV and Greeks. The EODHD Marketplace options API sits behind `DataSource.fetch_option_quotes`, and a synthetic source is the hermetic test path. `stonks options ingest` fills the lake. |
| 17.2 Pricing and Greeks | Done. A `PricingModel` seam over QuantLib: Black-Scholes with a dividend yield, Black-76, and the Barone-Adesi-Whaley, Bjerksund-Stensland and binomial American models. Implied vol that cannot be solved is empty, never a guess. A basic volatility surface interpolates the smile and total variance. |
| 17.3 Backtesting options | Done. A separate options backtest (`backtest/options_engine.py`): an options ledger with multipliers and position groups, fills from the next day's quotes with a spread share, all-or-none combo orders, expiry with exercise by exception, physical and cash settlement, early assignment through an `AssignmentModel` with risk flags, and split and dividend handling. |
| 17.4 Risk for options | Done. Portfolio and position Greeks, max loss of any structure, Reg T strategy-based and risk-based margin (`options/risk.py`), and four registered rules, all off: `option_greek_limits`, `option_max_loss`, `option_margin` and `short_option_guard`. They check a combo as one unit. |
| 17.5 Options strategies | Done. `covered_call`, `cash_secured_put` (with the wheel), `protective_put`, `vertical_spread` and `vol_premium_condor`, each with a hypothesis, built from a structure registry and a leg selector. `stonks options backtest --validate` runs the survival tests that apply: out of sample PSR, deflated Sharpe, wider fills, missing quote days and doubled fees. |

## Phase 18: Review, polish and prove it

The last phase. The whole project is reviewed file by file, fixed, tested through every edge case, and the console gets its own visual identity. It is done only when every gate below passes.

**Gates**

- Every finding from the review sweep is fixed with a test, or closed with a written reason.
- Python line and branch coverage of at least 90 percent overall and 95 percent in `core/`, `production/`, `execution/`, `auth/` and `portfolio/`, enforced in CI. Mutation testing on the money paths (orders, fills, risk, ledger, P&L) keeps a surviving-mutant rate under 10 percent.
- Pyright strict on `core/`, `production/`, `execution/`, `auth/` and `portfolio/`, and basic everywhere else. No ruff ignores without a comment.
- Every user journey passes end to end in a real browser against a real server on seeded data, on desktop and on a 375px phone.
- Zero axe accessibility violations. Lighthouse mobile scores of at least 95 for performance, accessibility and best practices.
- Every backend capability is reachable from the console, the CLI and MCP, or is listed as deliberately CLI-only. A parity test checks this.

| WP | Scope |
|----|-------|
| 18.1 Review sweep | Independent reviewers read every module in six areas (data and storage, research, trading and operations, API and security, console, tests and tooling) for bugs, edge cases, dead code, naming, duplication and missing tests. Findings are ranked and tracked. |
| 18.2 Fix waves | Fix every finding test-first, in waves with disjoint file ownership, then re-review what changed. |
| 18.3 Edge cases and test strength | Property tests (Hypothesis) for the money paths, fault injection (vendor errors, broker rejections, crashes mid-tick, disk full, clock skew, DST and holidays), coverage and mutation gates in CI. |
| 18.4 End to end | Playwright journeys over a seeded stack: first sign-in with 2FA, connect a broker (fake provider), build a universe, run a lab, promote through the gate and the override, paper tick, notifications, kill switch and resume, backup and restore. A CLI golden run and a Compose smoke test. |
| 18.5 Console identity | A visual identity specific to Stonks (type, colour, motion, data display, empty states, copy voice), applied to every page. Plain words for traders, no template look. |
| 18.6 Usability and polish | Walk every flow as a new trader, cut steps and jargon, fix copy, loading and error states, mobile, accessibility and performance until the gates pass. |
| 18.7 Feature completeness | A capability matrix of API, console, CLI and MCP. Fill every gap and add the parity test. |
| 18.8 Release | Changelog, docs and wiki final pass, version 1.0 tag and a deploy dry run. |

**Gate status (integration step 7, measured 2026-09-27)**

CI enforces each gate at today's value where it is still below the target, so it passes now and the floor only moves up. Raise a floor in the same change that lifts coverage.

| Gate | Target | Today | Enforced by |
|---|---|---|---|
| Coverage overall (coverage.py's combined line and branch number) | 90 | 94.3 (lines 95.8, branches 88.0) | `fail_under = 90` in `pyproject.toml` |
| Coverage `core/` | 95 | 98.5 | floor 95 (the target) in `tools/coverage_gate.py` |
| Coverage `production/` | 95 | 95.9 | floor 95 |
| Coverage `execution/` | 95 | 96.8 | floor 95 |
| Coverage `auth/` | 95 | 98.5 | floor 95 |
| Coverage `portfolio/` | 95 | 96.5 | floor 95 |
| Pyright strict over `core/` and `execution/` | 0 errors | 0 | `strict` in `[tool.pyright]`; `tools/pyright_gate.py` fails on any error under a strict path, baselined or not |
| Pyright basic over `src/stonks` | 0 errors | 489 errors, all in the baseline (9.5.6 fixed 4 and added none) | `tools/pyright_gate.py` fails on any error not in `tools/pyright-baseline.json` |
| Property tests (Hypothesis) | every money-path invariant | orders, ledger, fills, costs, risk rules, price adjustment | `tests/property/`, derandomized in CI (`HYPOTHESIS_PROFILE=deep` for 5000 examples) |
| Surviving mutants on the money paths | under 10% | 19.8% over six targets (the risk rules still to run in full) | `tools/mutation.py`, weekly and manual (`.github/workflows/mutation.yml`) |
| Ruff | no ignore without a comment | met | `[tool.ruff.lint]`, every ignore says why |
| End to end, desktop and 375px phone | every journey passes | 40 passed (20 per viewport), 2 skipped (no known axe issue to recheck), 0 xfail | `uv run pytest -m e2e tests/e2e`, `.github/workflows/e2e.yml` |
| axe violations | 0 | 0 on every page, both viewports, admin and trader | `test_accessibility.py`, `KNOWN_AXE` is empty |
| Lighthouse mobile, main pages (18.6) | 95+ performance, accessibility, best practices | Today 98/100/100, Strategies 99/100/100, Insights 98/100/100, Orders 98/100/100, Trade costs 98/100/100, Chart 99/100/100 (HTTPS, HTTP/2 and compression as in production; 91 to 96 performance over plain HTTP/1.1) | measured by hand, see docs/ui.md "Lighthouse budget" |
| Console copy | trader words only | no tick, ingest, shadow, promote, register or retire in trader prose | `npm run lint` runs `scripts/check-copy.mjs` |

First mutation run per target (cosmic-ray, mutants inside type annotations skipped as equivalent):

| Target | Module | Mutants run | Killed | Survived | Surviving |
|---|---|---|---|---|---|
| client_ids | `execution/orders.py` | 21 | 19 | 2 | 9.5% |
| orders | `portfolio/orders.py` | 397 | 282 | 63 | 18.3% |
| fills | `backtest/fills.py` | 393 | 188 | 108 | 36.5% |
| risk | `production/risk.py` | 157 | 157 | 0 | 0% |
| ledger_sync | `execution/reconcile.py` | 80 | 58 | 20 | 25.6% |
| pnl | `production/pnl.py` | 184 | 127 | 12 | 8.6% |
| rules | `production/rules/` | 136 of 2485 (sample) | 85 | 37 | about 30% |

"Mutants run" includes incompetent ones (code that no longer runs), which count neither way. The rate is survived over killed plus survived: 205 of 1036, 19.8%. The first run found a real gap: the non-default portfolio branch of `make_client_id` had no unit test (now covered, 47.6% to 9.5%). The risk rules have about 2500 mutants, too many for a local run. The weekly job measures them. Next: kill the surviving fills, orders and ledger mutants with tests until every target is under 10%.

Pyright strict plan. Strict mode comes one package at a time, smallest first, each in its own change that also shrinks the baseline: `core/` and `execution/` (done, BL-49), then `auth/`, `portfolio/` and last `production/`. Each step adds the package to `strict` in `[tool.pyright]`. Most strict errors are unknown types from untyped libraries (pandas, alpaca-py, exchange_calendars, pywebpush), so each step adds `pandas-stubs` or a typed wrapper at the seam and uses `dict[str, Any]` instead of bare `dict`. The basic-mode baseline is burned down alongside: pandas `itertuples()` rows, constructor settings read from the base class, and pydantic models built with no arguments.

## Phase 19: Go live with real money

**Status:** wave 1 done (19.1, 19.4, 19.6, 19.7), with its console screens: Live settings per live portfolio (allocation, account profile, and the live rules read only), broker gateway health on Health, and the fine order state in the orders views. Then 19.2 (the IBKR adapter) and 19.5 (reconciliation and drift, with the reconcile reports on Health). Design: `docs/design/live-trading.md`.

Stonks moves from simulated paper to real orders at Interactive Brokers, in stages. IB Gateway runs headless in Docker next to Stonks, and `ib_async` sits behind the `Broker` and `BrokerConnection` seams. Alpaca stays off. The IBKR login lives only in the gateway container's secret files. The account location is not decided, so account rules for the US and for the EU and UK are built and chosen per portfolio. The IBKR account is shared with the owner's own trading: Stonks only trades the positions it opened. The first live account is a cash account, long only.

| Stage | Broker | Gate to leave it |
|-------|--------|------------------|
| 0. Simulated paper | `SimulatedBroker` (today) | Go-live passed, 20 paper days, fake-gateway and live contract tests green, runbooks written. |
| 1. Broker paper | IBKR paper account | 20 trading days, last 4 weeks clean, a weekly re-auth, a restart and a disconnect survived, kill switch and outage drills, no duplicate orders. |
| 2. Live small | IBKR live account, a small allocation the owner sets by hand, tight caps, auto (approve mode optional) | 8 weeks, last 6 clean, 30 or more live fills, TCA gap not above the cost model. |
| 3. Scale up | Same. The owner raises the allocation by hand. | None. A bad week alerts but never cuts the allocation. |

A clean week has no unresolved reconciliation drift, no stuck orders, rejections under 2%, the gateway up for every submit window, no safeguard halt, and every fill with its commission. Stages move up only by a logged human action with a fresh second factor.

| WP | Scope | Owns | Status |
|----|-------|------|--------|
| 19.1 Broker seam for live | Stop price, time in force and outside-hours fields on `Order`, new optional broker capabilities (global cancel, account state, what-if margin, executions, quotes), execution ids and fee currency on fills, `orders.broker_ref`, an order state machine with an `unknown` state for timed-out orders and a startup reconciliation gate, a `Clock`. | `core/types.py`, `execution/brokers/base.py`, `execution/reconcile.py`, a new SQLite migration | done (migration 028) |
| 19.2 IBKR adapter | `IbClient` protocol over `ib_async`, session thread and reconnects, contract resolution with a `conId` cache, order mapping (`orderRef` idempotency, collared opening-auction limits, no outside hours), error mapping, account safety check, `FakeIbGateway`. | `execution/brokers/ibkr/*`, `tests/fakes/ib_gateway.py`, a new SQLite migration | done (migration 029) |
| 19.3 IBKR connection and borrow | `ibkr` provider with trade and short capabilities, sync, borrow quotes, daily borrow rates into the lake, optional Flex statements. | `connections/providers/ibkr.py`, `execution/brokers/ibkr/{borrow,flex}.py`, `ingest/sources/ibkr_borrow.py`, a new DuckDB migration | done (DuckDB migration 021) |
| 19.4 Gateway deployment | `ibkr-paper` and `ibkr-live` Compose profiles on an internal network, Docker secrets, weekly re-auth reminder, broker health and metrics. | `deploy/compose.yaml`, `deploy/ibkr/*`, `production/broker_health.py`, `scheduling/metrics.py` | done |
| 19.5 Reconciliation and drift | Start-of-day, submit and end-of-day checks, drift reports, `broker_drift` halt, auto pause on drift, short outage versus fault. | `execution/drift.py`, `production/live/checks.py`, `production/auto_pause.py`, `production/halts.py`, a new SQLite migration | done (migration 034) |
| 19.6 Live safeguards | An allocation cap the owner sets by hand (`capital_ramp`), per-order and per-day notional caps, fat-finger price bands against the last trade and NBBO, max orders per run with a `runaway` halt, and per-strategy protections (cooldown after a stop-out, pause after N stops, lock on a losing ticker). Settings under `[production.risk.rules.*]`, off by default. | `production/rules/{__init__,capital_ramp,live_caps,price_band,max_orders}.py`, `production/rules/settings.py`, `production/live/{settings,quotes,context,allocation,runaway}.py` | done |
| 19.7 Account rules engine | Account profiles per portfolio, cash-account rules (settled cash only, no free-riding, no shorts, no margin), US rules (pattern day trader, wash sales, Reg SHO), EU and UK rules (PRIIPs, short disclosure), settlement per market, buying power, FX funding. Transaction taxes in the cost model moved to a later wave. | `accounts/rules/*`, `production/rules/account_rules.py`, a new SQLite migration | done (taxes open) |
| 19.8 Tickets, approve mode and submit | `approve` mode between paper and auto (optional), order tickets with step-up approval in the console and by push (and `stonks tickets` with a typed phrase in the shell), decide after the close and submit in the window before the open (`live_submit` job), the startup reconciliation gate and the live context in the tick, runaway closes held as tickets, the tick's order rows in the fine order state. | `accounts/{models,subscriptions}.py`, `production/{tickets,submit,tick}.py`, `app/tickets.py`, `api/routers/tickets.py`, `web/src/app/pages/tickets/*`, a new SQLite migration | done (migration 033) |
| 19.9 Stages, gates and preview | Stage state machine and audit, daily gate metrics, gate reports, `stonks live stage` and `stonks live preview` (what-if, never transmits). | `production/live/{stages,gates,preview}.py`, `app/live.py`, `api/routers/live.py`, `web/src/app/golive/*`, a new SQLite migration | planned |
| 19.10 Protective stops | Optional GTC broker-side stops after entries, resized and cancelled with the position. | `production/live/stops.py` | planned |
| 19.11 Live tests, drills and runbooks | Live contract tests against the paper account (DU only: place and cancel a far limit, what-if, quotes, executions, reconnect), the paper soak report (`stonks live soak-report`), a manual reconcile (`stonks live reconcile`), the kill switch dry run (`stonks halts drill`, scratch state and simulated broker), runbooks for broker outage, stuck order, drift, re-auth, the kill switch and restore. No `kill_switch_drills` table yet: keep the drill's `--json-out` report. | `tests/integration/live/test_ibkr_live.py`, `production/soak.py`, `production/drills.py`, `app/drills.py`, `cli_live.py`, `docs/runbooks/*` | done |
| 19.12 Go live | Run the stages and gates. Operations only. | none | planned |
| 19.13 Margin accounts | Follow-up after the cash account runs well: a margin profile with longs and shorts at IBKR, borrow and locates through the adapter, buying power from what-if margin, the pattern day trader rule in force. The seams are ready (`account_type`, `short_permission`, `buying_power`, `reg_sho`). | `accounts/rules/*`, `execution/brokers/ibkr/*` | planned |
| 19.16 Broker edge cases | London and other pence-quoted markets in pounds (every price through the contract's price magnifier), hard to borrow short sales held as approval tickets even in auto, opening-auction orders cancelled by hand read as `cancelled` (not `expired`), a live test that measures how long an `orderRef` the gateway keeps. | `execution/brokers/ibkr/{contracts,orders,broker,status,ib_async_client}.py`, `production/{tickets,tick}.py`, `execution/borrow.py`, a new SQLite migration | done (migration 036) |

Waves: 19.1, 19.4, 19.6 and 19.7 first (done), then 19.2 and 19.8, then 19.3, 19.5, 19.9 and 19.10, then 19.11 and 19.12, then 19.13. Approve each trade is an optional mode, not a stage. The owner provides the IBKR account and its paper account, a secondary API username with IBKR Mobile for 2FA, market data subscriptions, the account type and client class, the capital to allocate, and 1 GB more VM memory per gateway.

## Phase 20: Complete product

Decided with the owner on 2026-09-27. Stonks stays private: the owner plus invited traders, no billing or public sign-up. Research data comes from EODHD All-in-one, live prices from Interactive Brokers.

**Status:** the backend of 20.1 to 20.5 is done: services, API, CLI and MCP, with minimal console services. The console screens come next, after the usability pass. 20.6 is planned.

**Console:** the screens for 20.1 to 20.3 and the draft approvals of 20.4 are done: the New order and Drafts tabs under Orders, Price alerts under Notifications, and Telegram in Settings (see `docs/ui.md`). The Calendar and Screener pages of 20.7 and 20.8 are done too, with the earnings warning on the order ticket.

**Console, 20.4 and 20.5:** done. The assistant chat (`/assistant`: streamed answers, tool steps, the yes or no step for writes, research only, the trace, unfreeze, conversations, and a clear page when it is off), cash flows (`/insights/cash-flows`), tax settings with lot picks and the yearly CSVs (`/insights/tax`), and time-weighted and money-weighted returns with base-currency values on Insights and Today. See `docs/ui.md`.

- 20.1: `production/manual.py`, `app/manual_orders.py`, `/api/orders/manual`, `stonks orders`, the MCP tools `place_order`, `change_order` and `cancel_order`. The order ticket and the account rules arrive with Phase 19 as registered risk rules, which manual orders already run. A real-money book needs a fresh second factor (`orders.live`). The tick never trades a manual holding.
- 20.2: `price_alerts/`, `app/price_alerts.py`, `/api/price-alerts`, `stonks price-alerts`, five MCP tools, and the `price_alerts` scheduler job after the price ingest. Live prices can call the same check once intraday lands.
- 20.3: `telegram/` (the channel, the long-polling bot, one-time link codes), `/api/telegram`, `stonks telegram`. Off unless `[telegram] enabled` and `STONKS_TELEGRAM_BOT_TOKEN` are set.
- 20.4: `assistant/` (the `ChatModel` seam, an OpenAI-compatible client, the agent loop over the MCP tools, conversations), `/api/assistant` with streamed answers. Off unless `[assistant] base_url` is set. The safety envelope: order drafts only (`/api/orders/drafts`, approved in the web app with a fresh second factor), research only by default, tickers resolved through `search_instruments`, untrusted tool results, a small default tool set with categories, a write rate limit that freezes, a recorded trace per turn, and an eval set (`stonks assistant eval`) with a planted prompt injection.
- 20.5: lake `fx_rates` (019) from EODHD forex, `fx/`, base-currency values in the portfolio, P&L, insights and TCA views, time-weighted and money-weighted returns from recorded deposits and withdrawals (`/api/portfolios/{id}/cash-flows`, `stonks cash-flows`), and `tax/` with the yearly CSV exports (`/api/tax`, `stonks tax`, `stonks ingest fx`). See `docs/tax.md`.

| WP | Scope |
|----|-------|
| 20.1 Manual orders | Place, change and cancel your own orders from the console, MCP and CLI, next to what strategies do. Each goes through the order ticket, every risk rule, the kill switch and the account rules, and is recorded with its reason and attribution `manual`. |
| 20.2 Price alerts | Alerts when a ticker crosses a level or moves by a percent over a window, on watchlists or single tickers, checked on each data refresh (and on live prices once intraday lands), sent through push, email and Telegram with quiet hours. |
| 20.3 Telegram bot | A notification channel plus commands: status, today, positions, signals, and the kill switch with a typed confirmation. Each chat is linked to one user with a one-time code, and every command respects that user's permissions. |
| 20.4 AI assistant | An in-app chat that talks to any OpenAI-compatible model endpoint (the owner's own open-source model on the local server through Ollama, vLLM or llama.cpp) and acts through the existing MCP tools as the signed-in user. Write actions need the same confirmations as the console, step-up actions stay in the web app. |
| 20.5 Currency and tax per portfolio | Each portfolio picks a base currency. FX rates in the lake, values and P&L converted, and yearly tax exports (realized gains per lot with FIFO or specific lots, dividends, withholding) for US and EU rules. |
| 20.6 Deploy anywhere | The same stack on a cloud VM or a local home server: one Compose file with profiles, a local-server guide (Tailscale, auto start, UPS and power loss, backups off the machine), and a cloud guide, with the lab worker and the model server optional. |
| 20.7 Calendars and news | Earnings, dividend and economic calendars from EODHD, a news and sentiment panel in the console (the data is already in the lake), a warning on an order ticket when earnings fall before the next open, and alerts on these events. Backend done: lake tables, the EODHD adapter, the daily `calendars_refresh` job, scoped reads, the earnings check and the event alerts, over the API, CLI and MCP (`docs/calendars.md`). Console done: the calendar page (`/calendar`) with a scope picker, window, countries and tabs, the news and sentiment panel, the earnings warning on the order ticket, and the event alerts in the alert settings (`docs/ui.md`). A switch per alert kind needs a server preference first. |
| 20.8 Screener | Saved screens on fundamentals and price rules, built on the universe rule provider, savable as a universe for the lab, and an MCP tool. Backend done: point-in-time metrics behind a registry, screens as `rule` universes, saved screens, API, CLI and MCP (`docs/universes.md#screener`). Console done: the screener page (`/screener`) with the metric picker, filters, results, saved screens and Save as a universe in rule or snapshot mode (`docs/ui.md`). |

## Phase 21: Intraday trading

Streaming prices (EODHD websockets, IBKR), a live event engine that decides on minute bars, intraday strategies with realistic fills and session rules, intraday risk (per-minute loss limits, halts), and the monitoring an always-on intraday loop needs. Design: `docs/design/intraday.md`.

**Status:** 21.1 done. Real intraday trading still waits for live daily trading to be stable.

| WP | Scope | Owns | Status |
|----|-------|------|--------|
| 21.1 Streaming data | The `StreamingSource` seam and registry (`eodhd` websockets, `ibkr` over the adapter's quotes, `replay`), ticks to 1m bars with the `bars` columns, idempotent writes into the `BarStore`, a Parquet recorder and replayer, and a supervised runner (reconnect with backoff, gap backfill through the REST intraday ingest, health metrics). Off by default (`[streaming]`). | `core/stream.py`, `streaming/*` | done (no migration) |
| 21.2.1 Event driver | `EventDriver` over any `StreamingSource`, the `FakeClock` hand-off, bar-close dispatch, and a source that replays lake bars for the backtest. | `engine/driver.py`, `streaming/sources/lake_bars.py` | planned |
| 21.2.2 Decision step | Decide on a bar close through `Strategy.decide` and a minute point-in-time lake, then `build_orders` per book. The intraday backtest runs on the driver. | `engine/step.py`, `backtest/intraday.py` | planned |
| 21.2.3 Intraday router and fills | Orders through the order state machine, next-bar fills with the participation cap and half spread, day orders at IBKR, reconciliation of intraday fills. | `engine/router.py`, `backtest/fills.py`, `execution/brokers/ibkr/orders.py` | planned |
| 21.2.4 Session rules | Regular hours only, no entries at the open and close edges, flatten before the close, per-ticker trading halts, early closes. | `engine/sessions.py` | planned |
| 21.2.5 Engine process | The always-on process, scheduler jobs around the session, startup reconcile, restart and state recovery. | `engine/process.py`, `scheduling/jobs.py` | planned |
| 21.3.1 Intraday strategies | Opening range breakout, VWAP reversion and intraday momentum with hypothesis cards, lab windows by session. | `strategies/examples/intraday_*.py`, `lab/dataset.py` | planned |
| 21.3.2 Intraday risk | Per-minute loss limit and the `intraday_loss` halt, intraday drawdown scaling, orders per minute cap, stale data gate, kill switch per event. | `production/rules/intraday_*.py`, `production/halts.py`, a new SQLite migration | planned |
| 21.3.3 Live marks and P&L | Minute marks from the stream, intraday P&L per book and sleeve, intraday risk snapshots. | `production/intraday_pnl.py`, a new SQLite migration | planned |
| 21.3.4 Monitoring | Stream and engine metrics, engine dead-man, event to order latency, alerts, a live console panel. | `scheduling/metrics.py`, `api/routers/stream.py`, `web/src/app/pages/live/*` | planned |
| 21.3.5 Intraday TCA | Spread from recorded quotes, arrival at the next minute, cost calibration for minute trading. | `production/tca.py`, `backtest/costs.py` | planned |

Waves: 21.1 first (done), then 21.2.1, 21.2.4 and 21.3.1, then 21.2.2, 21.2.3 and 21.3.2, then 21.2.5, 21.3.3, 21.3.4 and 21.3.5.

## Phase 22: Research depth

From the competitor study of 44 open-source projects (Qlib, alphalens, vectorbt, pysystemtrade, freqtrade and others). Stonks leads on validation; these close the gaps in factor research, risk and model lifecycle.

**Status:** 22.1 and 22.5 done. The console draws the heatmap with the plateau verdict in the lab run result, and the lab form offers Optuna, its samplers, pruning and the new objectives.

| WP | Scope |
|----|-------|
| 22.1 Optuna tuner and objectives | An Optuna tuner behind the Tuner seam, seeded and parallel, with every trial in the ledger, plus Sortino, Calmar, drawdown-penalised and multi-metric objectives. |
| 22.2 Factor layer | Done. A Factor ABC and registry, a small expression language compiled to DuckDB SQL, cached date-by-ticker panels, and a FactorStrategy. Modelled on Qlib's expression engine. `stonks factors`, `/api/factors`, MCP tools. The console has a factor library, factor pages, a formula editor with a live check, values on a date and a lab run of the factor strategy (Lab, Factors). |
| 22.3 Factor tear sheets | Done. alphalens-style IC by sector, asset class and size, returns per quantile, factor alpha and beta, and a monthly IC heatmap, for any factor. `stonks factors tearsheet --html`, a tear sheet job in the API and MCP, and a tear sheet runner on each factor page in the console. |
| 22.4 Factor risk model | Done. The `pca` and `style` covariance estimators (`portfolio/factor_model.py`), style exposures from the factor library read point in time (`factors/style.py`, a new `size_dv_60` factor), the `style_exposure` risk rule (off by default, tighten only), and factor attribution of P&L on every backtest tear sheet (`reporting/factor_attribution.py`). |
| 22.5 Sweeps and heatmaps | Vectorised sweeps for more strategies, with parameter heatmaps in reports and the console, linked to the plateau test. |
| 22.6 Model lifecycle | Scheduled retraining for ML strategies, model versions under one strategy id, new fits run as model books, swaps only through governance. Done: the weekly `model_retrain` job on all three backends, candidate and live version books in the tick, the swap check and an audited swap (`stonks registry versions|retrain|swap-check|swap|reject`, REST and MCP), and the console's Model versions tab on each strategy and the admin page `/ops/models`. See `docs/model-lifecycle.md`. |
| 22.7 Forecast weights | Carver-style forecast weights estimated net of costs, and rules dropped when too costly for an instrument. Done: the `ForecastWeightEstimator` seam (`handcraft`, `bootstrap`, `equal`), the speed limit, the `forecast_blend` strategy and a tear sheet section. See [forecast weights](strategies/forecast-weights.md). |
| 22.8 Factor library | Done. An Alpha158-style factor set with a next-open label, plus the fundamentals scores as factors. `stonks factors dataset` exports features and the label for a model. |
| 22.9 AI research loop | The assistant proposes hypotheses and runs lab trials under a budget, each counted in the trial ledger. Done: `assistant/research.py`, `POST /api/assistant/research`, the `start_research` MCP tool, SQLite `030_research_loop`, research cases in `stonks assistant eval`. Only validation windows after the model's training cutoff (`[assistant.research] model_cutoff`) count, budgets are enforced in code, and nothing registers. The console lists sessions, starts one, and shows each proposal with its budget use and lab run (Lab, Research sessions). |

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
10. Phase 18 runs last: the review sweep starts as soon as the code is frozen for review, the fix waves follow each merge wave, and the release waits for every gate.
11. Phase 19 follows `docs/design/live-trading.md`. Its code waves can start once Phase 18 has frozen the money paths, and real money waits for each stage gate.
12. Phase 17 (options) starts now, in parallel with Phase 19. Phase 20 runs alongside them. Phase 21 (intraday) follows once live daily trading is stable.
13. Phase 22 (research depth) follows the Phase 19 and 20 waves. Full comparison: the competitor study page.
