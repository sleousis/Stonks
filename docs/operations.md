# Operations

How to run Stonks every day and know when something breaks. Server setup, updates, off-server backups and host monitoring are in [deploy.md](deploy.md). Incident steps are in the [runbooks](#runbooks).

## The daily loop

```mermaid
flowchart LR
  I[ingest prices] --> T[tick] --> R[report]
  H[health, every 4 h]
  T --> N[alerts and notifications]
```

| Step | Command | What it does |
|------|---------|--------------|
| 1 | `uv run stonks ingest prices --tickers AAPL.US,MSFT.US` | Pulls the latest daily bars, checks them, upserts them. |
| 2 | `uv run stonks tick` | Scores active strategies, trades the default portfolio (`pf_default`), advances the model books, runs the hooks. |
| 3 | `uv run stonks report` | Writes the HTML report (`data/reports/report.html` by default). |
| 4 | `uv run stonks health --notify` | Checks data freshness and stuck or failed runs. Exits 1 and alerts when unhealthy. |

Run `uv run stonks db init` after every upgrade, before the first tick. It applies new migrations to both stores.

Every step is safe to rerun: ingest upserts, the tick reuses client ids for the same `as_of` and skips orders already placed (and model books already evaluated), health only reads. Times are UTC; `tick` defaults `--as-of` to today's UTC date and refuses a date older than its latest snapshot.

## Scheduler

The built-in scheduler runs the loop on the exchange calendar, catches up missed runs, alerts on missed deadlines and pings an external monitor.

```bash
uv run python -m stonks.scheduling run          # run until Ctrl+C / SIGTERM
uv run python -m stonks.scheduling next         # each job's next fire time
uv run python -m stonks.scheduling runs         # recent runs and their status
uv run python -m stonks.scheduling run-now tick --as-of 2026-09-25
uv run python -m stonks.scheduling check        # deadline check once; exit 1 on a miss
uv run python -m stonks.scheduling metrics      # Prometheus text metrics
```

Run exactly one, as a long-lived process (systemd unit, Windows service, or the `scheduler` container). It acts as `service:scheduler`. A second `run` finds `scheduler.lock` taken and exits with code 2.

### Default jobs

| Job | When (NYSE, trading days) | Deadline |
|-----|------|----------|
| `ingest_prices`: last 7 days of daily bars for `[production].universe` | close + 30 min | 60 min |
| `tick` | close + 45 min | 60 min |
| `report`: `reports/latest.html` next to the state DB | close + 90 min | none |
| `health` | every 4 hours | none |

These four are the only actions today. Backups, notification delivery and connection syncs are not scheduler jobs yet: run them from cron or a timer (see below).

Listing any `[[scheduler.jobs]]` in the config replaces the whole default list:

```toml
[scheduler]
catch_up = "latest"          # none | latest | all
max_catch_up_runs = 3
catch_up_window_hours = 72

[[scheduler.jobs]]
name = "tick"
action = "tick"
deadline_minutes = 60
ping_url_env = "STONKS_PING_TICK"      # healthchecks.io-style URL, kept in .env
trigger = { type = "session", calendar = "XNYS", anchor = "close", offset_minutes = 45 }

[[scheduler.jobs]]
name = "crypto_tick"
action = "tick"
params = { tickers = ["BTC-USD.CC", "ETH-USD.CC"] }
trigger = { type = "daily", at = "00:30", timezone = "UTC" }
```

Triggers:

- `session`: minutes after (or before) a calendar's `open` or `close`, on its trading days. The run is for that session's date.
- `daily`: a wall-clock time in an IANA time zone, optionally only on `weekdays` or a `calendar`'s trading days. DST gaps and repeats run once.
- `interval`: every `every_minutes` on a fixed grid from midnight UTC.

### Market calendars

Sessions come from `exchange_calendars` (wrapped in `scheduling/calendar.py`), with holidays, early closes and DST. Tickers map to calendars by suffix: `.US` is NYSE, `.LSE` London, `.XETRA` Xetra, `.COMM` CME, `.GBOND` a 24/5 calendar, and crypto (`.CC` or asset class `crypto`) trades 24/7. The tick and price ingest skip a day on which nothing in the universe trades. Turn that off per job with `params = { skip_closed_days = false }`.

### Backends

DuckDB allows one writer per lake, so `[scheduler].backend` picks where jobs run:

| Backend | Jobs run | Use when |
|---------|----------|----------|
| `api` | Through the running API (`POST /api/ingest/runs`, `POST /api/ticks`, then polling `GET /api/jobs/{id}`). Never opens the lake. | `stonks serve` runs, as in the Compose stack. |
| `in_process` | Inside the API process, on its job runner. | A single-process install. `stonks serve` starts and stops it with the app. |
| `local` | In the scheduler process, opening the stores like the CLI. | Nothing else holds the lake. |

`auto` (default) picks `api` when `STONKS_API_URL` or `[scheduler].api_url` is set, else `local`. The `api` backend sends `STONKS_API_TOKEN`, only over https, to loopback, or to hosts in `[scheduler].api_trusted_hosts` or the comma-separated `STONKS_API_TRUSTED_HOSTS` (Compose sets `api`).

### Runs and catch-up

Each fire is one `scheduled_runs` row keyed by job and run key (session date, local date, or UTC instant), so a fire never runs twice, even across restarts or two schedulers. At start, `catch_up` decides what happens to fires missed within `catch_up_window_hours`: `none` drops them, `latest` (default) runs the newest, `all` runs up to `max_catch_up_runs`, oldest first. A job that never ran has nothing to catch up.

A failed run is not retried. It alerts; rerun it with `run-now`. A run interrupted by a crash is marked `failed` ("interrupted") at the next start. If the scheduler stops while an API job runs, the job keeps going in the API; check `GET /api/jobs/{id}` before rerunning.

### Dead-man checks

- **Deadlines.** A watchdog checks every `watchdog_seconds` that each job with `deadline_minutes` succeeded (or was skipped) in time. A miss sends one error alert, recorded in `scheduler_deadline_alerts` so restarts don't repeat it.
- **Pings.** With `ping_url_env`, a job POSTs `<url>/start`, then `<url>` or `<url>/fail`. The external monitor alerts when pings stop, which also covers a dead scheduler or server. Logs show only `scheme://host/***`.

## Health and metrics

`stonks health` exits 0 when healthy and 1 otherwise. Thresholds are under `[production.health]`:

| Check | Fails when | Setting (default) |
|-------|------------|---------|
| `freshness:<ticker>` | The latest daily bar is older than N calendar days, or missing. | `max_bar_age_days` (4) |
| `stuck_ticks` | A tick has been `running` for more than N minutes. | `stuck_tick_minutes` (60) |
| `stuck_ingest_runs` | An ingest has been `running` for more than N minutes. | `stuck_ingest_minutes` (180) |
| `ingest_failures` | An ingest failed in the last N hours. | `ingest_failure_lookback_hours` (24) |

The API serves `GET /api/health` (liveness, used by Docker and Caddy) and `GET /api/health/report` (the full report).

`python -m stonks.scheduling metrics` prints Prometheus text from the state DB: tick counts, duration and last success; orders by status and rejections; API jobs and queue depth; each scheduled job's last success, last status and next run; the scheduler heartbeat. `--data-age` adds universe data age buckets but opens the lake, so use it only when `stonks serve` is not running.

`stonks serve` serves the same set, data age included, at `GET /metrics`. Scrapes from a loopback peer need no token. From anywhere else they need the scrape-only bearer token `STONKS_METRICS_TOKEN`. The API token is not accepted there, so Prometheus never holds an admin credential. `STONKS_METRICS_ALLOW_LOOPBACK=false` requires the token on loopback too.

A useful alert: `time() - stonks_scheduled_job_last_success_timestamp_seconds{job="tick"} > 26 * 3600` on weekdays.

Over HTTP, `GET /api/health/live` (the process, plus the scheduler when `stonks serve` hosts it) and `GET /api/health/ready` (state migrated, lake present) are open to everyone, like `GET /api/health`. They answer 200 with each check's name and `ok`, or 503 naming the failing checks; details go to the log, not the response.

With `[scheduler] backend = "in_process"`, `stonks serve` starts the scheduler with the app and stops it (after the running job) on shutdown. `GET /api/schedule` lists the jobs, their next fire and recent runs; `POST /api/schedule/{job}/run-now` (token, audited as `schedule.run_now`) starts a `manual:` run in the background.

## Backups and restore

```bash
uv run python -m stonks.ops backup             # backup, verify, prune
uv run python -m stonks.ops list
uv run python -m stonks.ops verify <backup-id>
uv run python -m stonks.ops restore <backup-id> --data-dir /srv/stonks/data-restored
uv run python -m stonks.ops prune
```

A backup is one folder `stonks-<UTC time>Z` with the state DB, the lake, Parquet bars (hard-linked), artifacts and a `manifest.json` of hashes and row counts. It goes to `[backup].dir`, or `backups/` next to the lake. Pruning keeps the newest backup of each of the last 7 days, 4 weeks and 12 months.

The lake must not be held by another writer: stop `stonks serve` or back up from the server's own copy. `[backup]` is not read from the config file yet, so the defaults apply. Off-server encrypted copies (restic) are covered in [deploy.md](deploy.md#6-backups). Full steps: [runbooks/restore.md](runbooks/restore.md).

## Data quality

Every `ingest prices` and `ingest intraday` batch is checked before it is stored:

- **Quarantined** (kept out of `bars`, written to `quarantined_bars` with reasons): missing or non-positive prices, high below low, close outside the bar's range, duplicate timestamps, one-bar spikes that revert.
- **Warnings** (kept): extreme moves that stick, stale series, flat price streaks, zero-volume streaks.

Each run stores a summary in `ingest_runs.quality_json` and alerts when it quarantines a bar, when 5 or more tickers warn, or when a fallback source supplied data. Thresholds use the defaults in `ingest/quality_config.py`; `[ingest.quality]` and `[ingest.fallback]` are not read from the config file yet, and no command wires a fallback source today. Triage: [runbooks/data-stale.md](runbooks/data-stale.md).

## Alerts

Operator alerts go through the `Notifier` seam, set under `[notify]`:

```toml
[notify]
backends = ["log", "store", "webhook"]   # default ["log", "store"]; [] disables
min_level = "warning"                    # info | warning | error
```

- `log`: a `notify` event in the structured log.
- `store`: every alert, any level, in the `alerts` table, read by `GET /api/alerts`. Credentials are scrubbed first.
- `webhook`: POSTs `{level, title, message, fields, text}` to `STONKS_NOTIFY_WEBHOOK_URL` (keep it in `.env`; Slack and Mattermost show `text`).

A notifier never raises. What alerts:

| Source | Level | When |
|--------|-------|------|
| tick | error | The tick raised (recorded as `error`, exit non-zero). |
| tick | warning | Status `partial`, or the broker rejected orders. |
| `health --notify` | error | Any check failed. |
| scheduler | error | A job failed or missed its deadline. |
| ingest | warning | Quarantined bars, many warnings, or fallback used. |

Orders the risk rules clip or drop are not alerts; they are listed under `risk_adjustments` in the tick summary.

## Push and per-user notifications

Per-user notifications go through an outbox: the router writes one in-app `alerts` row and one delivery per channel (Web Push, the user's webhook, email), with dedupe, preferences and quiet hours in the user's time zone. The delivery worker sends them with retries and dead letters. The tick does not enqueue signals into the outbox yet (its `notification_enqueue` hook only logs), so today `notify test` is the way to exercise it.

```bash
uv run python -m stonks.notify vapid-keygen             # prints STONKS_VAPID_PUBLIC_KEY / _PRIVATE_KEY
uv run python -m stonks.notify test --user you@example.com
uv run python -m stonks.notify deliver                  # one delivery pass
```

Set `STONKS_VAPID_PUBLIC_KEY`, `STONKS_VAPID_PRIVATE_KEY` and `STONKS_VAPID_SUBJECT` (a `mailto:` or `https:` contact) as secrets. Email is optional (`STONKS_SMTP_HOST`, `_PORT`, `_USERNAME`, `_PASSWORD`, `_FROM`, `_SECURITY`). Rotating the VAPID pair makes every user re-enable push.

The scheduler does not run `deliver` yet, so run it every minute from cron or a timer. The console's install and push opt-in are described in [ui.md](ui.md#install-and-notifications-pwa).

## Broker connections

Connections sync a user's broker accounts read-only: positions, cash and activities, into a linked `broker` portfolio. No provider works until an admin enables it.

```bash
export STONKS_SECRET_KEYS="$(uv run python -m stonks.security keygen 2>/dev/null)"   # once; keep it secret
export STONKS_CONNECTIONS_ENABLED_PROVIDERS=alpaca
uv run python -m stonks.connections providers
STONKS_CONNECT_API_KEY=... STONKS_CONNECT_SECRET_KEY=... uv run python -m stonks.connections connect alpaca
uv run python -m stonks.connections link <connection> <account>
uv run python -m stonks.connections sync --due        # every due connection
uv run python -m stonks.connections rotate-keys       # after adding a new master key
```

- Credentials come from `STONKS_CONNECT_<FIELD>` or a hidden prompt, never from arguments, and are sealed with the master key from `STONKS_SECRET_KEYS`. Keep the old keys listed after a new one until `rotate-keys` has run.
- SnapTrade needs `STONKS_SNAPTRADE_CLIENT_ID` and `STONKS_SNAPTRADE_CONSUMER_KEY` and connects through its portal (`connect snaptrade --redirect URL`, then `callback`).
- A sync writes one `portfolio_snapshots` row per portfolio and day (`source` other than `tick`), and is safe to repeat.
- The scheduler does not run `sync --due` yet: run it from cron or a timer, a few times a day.

## Risk policy

Risk rules run between construction and the broker, configured under `[production.risk]`:

| Setting | Effect |
|---------|--------|
| `enabled` | `false` passes orders through untouched. |
| `max_open_positions` | Buys that would open a position beyond the count are dropped. |
| `max_weight_per_ticker` | Buys are clipped to this fraction of portfolio value. |
| `max_weight_per_asset_class` | For example `crypto = 0.2`. With any class cap set, buys of unknown-class tickers are dropped. |
| `cash_buffer_fraction` | Buys are clipped so this fraction stays in cash, net of costs. |
| `min_order_notional` | Smaller buys are dropped. |

Weights use portfolio value before the tick's orders. Sells are never blocked, only clipped to the held quantity, and go before buys. Portfolio and subscription overrides can only tighten the policy. The rules are a registry (`production/rules/`); the newer ones (`risk_per_position`, `portfolio_vol`, `drawdown_scaling`, `liquidity`, `sector_cap`, `max_holding`) are registered but off, since `[production.risk.rules]` is not read from the config yet.

## Halts and the kill switch

A halt stops new orders before they reach the broker. Every halt is a row in `risk_halts`, and the tick checks global, user and portfolio halts for each book.

```mermaid
flowchart LR
  B[circuit breaker] --> H[(risk_halts)]
  O[health: stale data or stuck run] --> H
  K[kill switch] --> H
  H --> G[tick gate]
  G -->|buys| S[sells and exits only]
  G -->|all| N[no orders]
```

| Halt | Trips when | Ends |
|------|------------|------|
| `month_loss` | Value is 6% below the month's first snapshot. | At the start of next month, or when cleared. |
| `week_loss` | Value fell 4% over 5 snapshots. | At the start of next month, or when cleared. |
| `drawdown` | Value is 20% below its peak. | Only when a person clears it. |
| `operational` | `stonks health` finds stale data or a stuck run. | When health passes again. |
| `kill` | A person turns on the kill switch. | Resume with the typed confirmation. |

- The breaker limits live in `[production.risk.rules.circuit_breaker]` (`max_month_loss`, `max_week_loss`, `max_drawdown_halt`, `cooldown`). They are off until set. The same rule runs in backtests.
- Breaker halts block buys. Sells and exits still go through.
- The kill switch has three scopes: `global` (admins), `user` (all your portfolios) and `portfolio` (one of yours). It stops every order, or only buys with `flatten`.
- Resume the kill switch with `POST /api/halts/{id}/resume` and the text `RESUME TRADING`. Clear other halts with `POST /api/halts/{id}/clear` and a reason.
- Every action writes an `audit_log` row. Every clear also writes a `risk_reset` row in `status_changes`.
- A trip sends a `risk` notification to the portfolio owner, or to the admins for a global halt.
- MCP can list halts and turn the kill switch on (with `confirm=true`). It cannot resume.

### Quit rule

After each tick the quit rule checks every active strategy. It sums the strategy's share of each portfolio's P&L since promotion. When the drawdown of that P&L passes 1.5 times the backtest drawdown, or the Monte Carlo 95th percentile when it is lower, the admins get an `error` notification. With `auto_demote` on, the strategy also moves to `shadow` with a logged reason.

## Model books (shadow mode)

`shadow` strategies are scored each tick and never traded. Each runs alone against a virtual portfolio seeded with `initial_cash`, with the same risk policy, always on a simulated broker:

- `shadow_decisions`: one row per hypothetical order.
- `shadow_portfolio_snapshots`: one row per strategy per `as_of`.

A failing model book is reported in the tick summary and never affects real books. Turn it off with `[production].shadow_enabled = false`; `--dry-run` writes nothing.

## Go-live and promotion

```bash
uv run stonks pnl --strategy <shadow-id>
uv run stonks golive check <shadow-id>          # exit 1 when a check fails
uv run stonks registry promote <shadow-id>
uv run stonks registry promote <id> --override --reason "why"
uv run stonks registry history <id>
```

With `[golive] incubation = true` the gate needs at least 63 days (or MinTRL, capped at 252), 20 trades, live results inside the Monte Carlo band, no quit-rule breach, stored reports for every `promotion` preset test, non-zero costs, a recorded hypothesis, and at least 30 backtest trades. The gate never changes status; `registry promote` does, and refuses without a pass or an override.

## Reading P&L

```bash
uv run stonks pnl                      # the default portfolio, from inception
uv run stonks pnl --since 2026-09-01   # rows from this day on
uv run stonks pnl --strategy <id>      # a model book
```

Columns: `date`, `value` (cash plus marked positions), `change`, `daily`, `cumulative` (since the first snapshot) and `drawdown` (below the running peak). `--since` only trims rows; `cumulative` and `drawdown` still count from inception.

## Trading costs and the journal

Every order the tick places records its decision: the price the strategy decided at (the latest close), the day, why it traded (the trigger, the strategy, the signal score and rank, the constructor and the target weight) and what the cost model expected it to cost. Each fill records its arrival price, the market price before costs.

```bash
uv run stonks tca summary                     # the default portfolio, all orders
uv run stonks tca summary --by strategy       # or ticker, portfolio, day, week, month
uv run stonks tca journal --since 2026-09-01  # orders with reason, outcome and notes
uv run stonks tca order <client-id>           # one order in full
uv run stonks tca note <client-id> "text"     # add a note
uv run stonks tca edit-note <note-id> "text"  # change your note
uv run stonks tca refresh                     # fill next-session prices from the lake
```

The summary shows implementation shortfall in basis points of the traded value at the decision price. Positive numbers are costs.

- `delay`: the move from the decision price to the arrival price.
- `impact`: the move from the arrival price to the fill price (spread, slippage, impact).
- `fees`: the fees charged.
- `IS`: all three together.
- `opportunity`: what the unfilled part cost, measured at the next session's close.
- `convention`: how much more the live fill paid than a backtest would have, which fills at the next session's open.
- `model` and `gap`: the cost model's estimate and the realised shortfall minus that estimate. A gap that stays above zero means the cost model is too cheap.

The next session's open and close arrive a day later. The tick fills them in after every run, and `stonks tca refresh` does it by hand. For an external broker the arrival price is that next open.

The go-live report shows the strategy's live shortfall next to the modelled cost. The same numbers are in the API under `/api/tca` and in the MCP tools `tca_summary`, `trade_journal` and `order_tca`. A trader only sees the orders of their own portfolios and edits only their own notes.

Backtests use the same math. `Backtester.decision_prices` holds the close each order was decided at, and `production.tca.backtest_shortfalls` prices the simulated fills against it.

## Without the scheduler

Plain cron works too. After the US close in UTC terms (22:30 UTC is safe all year):

```cron
30 22 * * 1-5  cd /opt/stonks && uv run stonks ingest prices --tickers AAPL.US,MSFT.US >> logs/ingest.log 2>&1
45 22 * * 1-5  cd /opt/stonks && uv run stonks tick >> logs/tick.log 2>&1
0  23 * * 1-5  cd /opt/stonks && uv run stonks health --notify >> logs/health.log 2>&1
*  *  * * *    cd /opt/stonks && uv run python -m stonks.notify deliver >> logs/notify.log 2>&1
0  */6 * * *   cd /opt/stonks && uv run python -m stonks.connections sync --due >> logs/sync.log 2>&1
0  2  * * *    cd /opt/stonks && uv run python -m stonks.ops backup >> logs/backup.log 2>&1
```

cron does not know exchange holidays; on those days the tick finds no new bars and trades nothing.

## Runbooks

- [Data stale or bad](runbooks/data-stale.md)
- [Back up and restore](runbooks/restore.md)
- [Deploy failed](runbooks/deploy-failed.md)
