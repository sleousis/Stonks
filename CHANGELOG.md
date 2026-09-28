# Changelog

All notable changes to Stonks. Versions follow [Semantic Versioning](https://semver.org).

Each release section is written by hand from the merge commits and pull request titles, grouped by area. `git cliff --unreleased` (with `cliff.toml`) lists the commits since the last tag to start from. The release workflow publishes the section of the tagged version, and falls back to the `git cliff` list when there is none.

## [1.0.0] - 2026-09-28

The first tagged release. It covers everything merged to `main` up to pull request #24. The biggest new parts since pull request #23 are the Interactive Brokers path to real money, the complete trader product, the intraday engine and deeper research tools. Live trading, intraday, options and the assistant are all off by default.

### Live trading

- Trade through Interactive Brokers: an IBKR adapter over `ib_async` behind the `Broker` seam, with a contract cache, the broker's what-if, day and good till cancelled orders and London prices in pounds.
- Run IB Gateway in Compose (`ibkr-paper` and `ibkr-live` profiles). The `broker_health` job probes it every 5 minutes, and a Sunday push reminds you to approve the weekly login.
- Move a portfolio to real money in stages: simulated paper, broker paper, live small, live scale. Each step has a gate report, and a dry-run preview shows the next orders without sending them.
- Approve each trade: the new `approve` mode writes order tickets, and the `live_submit` job sends the approved ones in a window before the open.
- Reconcile with the broker at the start of day, before a submit and at the end of day. Unexplained drift opens the `broker_drift` halt and pauses auto. An outage only skips the day.
- Check cash and IBKR Flex statements against Stonks' own fills, with drift metrics that feed the stage gates.
- Keep account rules for US and EU accounts, and live safeguards: live caps, a capital ramp, a price band, an order cap per run and the `runaway` halt.
- Place optional protective stops at the broker, one per position, sharing a one-cancels-other group with its exits.
- Hold a short sale of a hard to borrow name for a person, check daily borrow rates, and gap-check opening tickets against the pre-open quote.
- Give the API its own IBKR client id, so the kill switch and manual orders work while the tick holds a session.
- Prove it before real money: IBKR paper contract tests, a paper soak report, a kill switch drill and live runbooks.

### Product

- Place, change and cancel manual orders on your own portfolios. Every order goes through the halts, the risk rules and the broker, and the tick never trades a manual holding.
- Set price alerts on tickers and watchlists.
- Link Telegram and get notifications there. A bot acts as the linked user.
- Chat with an in-app AI assistant on a local model (Ollama, vLLM or llama.cpp). It acts as the signed-in user, can only draft orders, and freezes itself after a burst of writes.
- Track deposits and withdrawals, with time-weighted and money-weighted returns in the portfolio's base currency (FX rates in the lake).
- Export yearly tax files: realised gains by FIFO or specific lots, US wash sales and dividends.
- See earnings, dividend and economic calendars, news, an earnings warning before the next open, and economic release alerts by country and importance.
- Screen stocks on price and fundamental metrics, save screens, run whole-exchange screens as jobs, and turn a screen into a universe.
- Edit universes and read their membership history on the universes page.

### Intraday

- Stream live quotes into 1-minute bars behind a `StreamingSource` seam: EODHD websockets, IBKR quotes and replay of a recording.
- Decide on every bar close with the event driver and decision step. The intraday backtest runs the same code.
- Route intraday day orders through the order state machine, with minute fills in the simulated broker and IBKR day orders.
- Run the engine as its own process with crash recovery, stop files and `engine_start` and `engine_stop` jobs.
- Apply session rules, trading halts, intraday risk rules and the `intraday_loss` halt.
- Mark books live and track intraday P&L. Watch the stream and engine on the Live page, with a dead-man alert when no bar closes in market hours.
- Measure intraday trading costs and propose a calibrated cost block (never applied by itself).
- Three intraday strategies: opening range breakout, VWAP reversion and intraday momentum.

### Research

- Tune with Optuna (TPE, NSGA-II, random, pruning), score with risk-aware objectives, and see parameter heatmaps with the plateau verdict.
- Build factors: the `alpha158`, `classic` and `fundamentals` libraries, a formula language compiled to DuckDB SQL, factor tear sheets, model datasets and the `factor` strategy.
- Model risk with PCA and style factors, cap style exposure, and attribute P&L to factors.
- Weigh forecasts net of costs with the speed limit.
- Keep model versions: scheduled retraining into candidates that run as model books, and governed swaps.
- Run an AI research loop that proposes hypotheses under a trial and compute budget, validates only after the model's cutoff and never registers a strategy.
- Read sector labels and restated statements point in time, so nothing leaks backward.
- 32 example strategies, 6 wrappers and 22 survival tests in total.

### Options

- Research options, off by default and never in the tick: instruments, chains, QuantLib pricing, backtests, risk rules and five strategies.
- Price with a Treasury rate curve and only the dividends declared by the pricing day.
- Browse chains, payoffs, strategies and backtests on the console's Options page and through MCP.

### Console

- A trader workspace: first-run guide, watchlists, charts, leaderboard, tear sheets, ticker compare, rolling Sharpe, monthly heatmap, PDF printing and open lots.
- New pages and panels: Approvals, live settings, gateway health, order states, manual order ticket, drafts, price alerts, Telegram link, assistant chat, cash flows, tax, calendars, screener, factors, model versions and the Live page.
- Its own look (trading day spine, tape and ticket, brass means real money), trader words only, zero axe issues and Lighthouse mobile scores of 98 and up.

### Operations

- Deploy on a cloud VM or a home server from one Compose file, with profiles for the scheduler, lab worker, backups, both IB Gateways and the AI model server.
- New scheduler jobs: `ingest_borrow`, `price_alerts`, `broker_health`, `ibkr_reauth_reminder`, `live_sod_check`, `live_eod_check`, `live_submit`, `live_stops`, `live_gate_days`, `calendars_refresh`, `model_retrain`, `engine_start` and `engine_stop`.
- Engine and stream metrics on `/metrics`, and runbooks for broker outages, gateway re-login, stuck orders, reconcile drift and the kill switch drill.

### Security

- Real money needs a fresh second factor: approving tickets, live manual orders and resuming auto. The CLI asks for a typed confirmation.
- IBKR logins live only in the gateway's Docker secret files. Tokens for Telegram, the assistant and Flex come only from the environment.
- API tokens and MCP can preview live orders but not place them, and cannot approve tickets.

### Upgrading from pull request #23

1. Back up first: `uv run stonks backup backup`.
2. Stop the scheduler, the lab worker and any engine process, then install the new version (`uv sync`, or pull image `1.0.0`).
3. Run `uv run stonks db init`. It applies SQLite migrations 027 to 044 and DuckDB migrations 019 to 022. Migrations 033, 035, 039, 040 and 042 rebuild tables and keep every row.
4. New settings sections, all off or safe by default: `[production.live]` (with `.submit`, `.stages`, `.reconcile`), `[production.intraday_pnl]`, `[streaming]`, `[streaming.monitor]`, `[engine]`, `[factors]`, `[screener]`, `[lifecycle]`, `[lifecycle.swap]`, `[assistant]` (with `.envelope`, `.research`), `[telegram]` and `[sources.ibkr_borrow]`. `[brokers] kind` now accepts `ibkr`, set up under `[brokers.ibkr]`.
5. New environment variables, only for what you turn on:
   - `STONKS_TELEGRAM_BOT_TOKEN` for the Telegram bot.
   - `STONKS_IBKR_FLEX_TOKEN` for Flex statement checks.
   - `EODHD_API_KEY`, which streaming also reads for the EODHD websockets.
   - `STONKS_AI_BASE_URL` and `STONKS_ASSISTANT_API_KEY` for the assistant.
   - `IBKR_READ_ONLY_API`, `IBKR_TIME_ZONE` and `IBKR_AUTO_RESTART_TIME` for the IB Gateway containers.
6. For IBKR, create the secret files in `deploy/ibkr/secrets/` and add `ibkr-paper` to `COMPOSE_PROFILES` (see `deploy/ibkr/README.md`).
