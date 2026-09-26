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
| 5.2 Angular trader console | Angular (standalone components, signals) app in `web/`, typed client generated from the OpenAPI contract. Pages: dashboard (equity, P&L, drawdown, positions, health), strategies (status, survival reports, promote/retire with confirmation), lab (launch backtests and lab runs, live progress, equity curves), data (coverage, freshness, ingest runs, trigger ingest), orders and fills, shadow vs active comparison, go-live gate, alerts, settings. Keyboard-friendly, dark and light themes, accessible, responsive. | `web/` |
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

## Execution order

1. Wave 1 in parallel: backtest (1.2, 1.3, 3.5), lab (1.4, 1.5, 3.3), production (2.3, 2.4, 2.5), broker (2.1, 2.2), data (3.4), strategies (3.1, 3.2, 4.2), and the service layer plus REST API for existing features (5.1).
2. Merge, review the combined diff, fix confirmed findings.
3. Wave 2: broker in the tick (2.6), reporting (4.1), go-live gate (4.3), API routes for the Wave 1 features, MCP server (5.3), Strategy Studio backend (5.5).
4. In parallel with Wave 2: neurotrader888 ports (6.1 to 6.5) and book research (7.1 to 7.5).
5. Integration and cleanup (8.1 to 8.6), then the book synthesis (7.6).
6. Wave 3: Angular trader console (5.2) including the Strategy Studio UI (5.5), built against the finished API contract, then end-to-end tests (5.4), alongside the first book-driven improvements (7.7) and the port follow-ups (6.6).
7. Final review, fix, re-review.
