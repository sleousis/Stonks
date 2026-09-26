# Operations

How to run Stonks unattended: schedule the daily jobs, get alerted when something breaks, and read the P&L.

## The daily loop

Three commands, in this order, once per trading day after the close:

| Step | Command | What it does |
|------|---------|--------------|
| 1 | `uv run stonks ingest prices --tickers AAPL.US,MSFT.US` | Pulls the latest daily bars into the lake. |
| 2 | `uv run stonks tick` | Ranks active strategies, applies the risk policy, places orders, snapshots the portfolio, then evaluates shadow strategies against their virtual portfolios. |
| 3 | `uv run stonks health --notify` | Checks bar freshness and stuck or failed runs. Exits 1 and sends an alert when unhealthy. |

Run `uv run stonks db init` once after every upgrade, before the first tick. It applies new migrations to both stores (for example `002_shadow.sql`, which adds `shadow_decisions` and `shadow_portfolio_snapshots`). If you skip it, the tick still trades, but the shadow phase fails and records a `shadow_error` in the tick summary.

All three commands are safe to rerun:

- `ingest prices` upserts, so a rerun rewrites the same bars.
- `tick` builds client ids from `(as_of, strategy, ticker, side)`, so a rerun for the same day skips orders already filled. Shadow strategies already evaluated for that day are skipped too (`already_evaluated`).
- `health` only reads.

Timestamps are UTC, and `tick` defaults `--as-of` to today's UTC date.

## Scheduling

Stonks has a built-in scheduler, so you no longer write cron entries. It runs the daily loop on the exchange calendar, catches up runs it missed while it was down, alerts when a job misses its deadline, and pings an external monitor so you hear about it when the scheduler itself dies.

```bash
uv run python -m stonks.scheduling run          # run until Ctrl+C / SIGTERM
uv run python -m stonks.scheduling next         # each job's next fire time
uv run python -m stonks.scheduling runs         # recent runs and their status
uv run python -m stonks.scheduling run-now tick --as-of 2026-09-25
uv run python -m stonks.scheduling check        # deadline check once; exit 1 on a miss
uv run python -m stonks.scheduling metrics      # Prometheus text metrics
```

Run it as one long-lived process: a systemd unit, a Windows service, or the `scheduler` container of the Compose stack. It acts as the service account `service:scheduler`, which is recorded on every run.

### Where jobs run

DuckDB allows one writing process per lake file, and `stonks serve` holds the lake while it runs. So the scheduler has three backends, set by `[scheduler].backend`:

| Backend | Jobs run | Use when |
|---------|----------|----------|
| `api` | Through the running API: `POST /api/ingest/runs` and `POST /api/ticks`, then polling `GET /api/jobs/{id}`; health reads `GET /api/health/report`. The scheduler process never opens the lake. | `stonks serve` runs, as in the Compose stack. |
| `in_process` | On the API process's own job runner, sharing its lanes, so an ingest never overlaps another lake write. `stonks serve` starts it. | A single-process install. |
| `local` | In the scheduler process, opening the stores the way the CLI does. | Nothing else holds the lake, for example a dev box without `stonks serve`. |

`auto` (the default) picks `api` when an API URL is set (`STONKS_API_URL` or `[scheduler].api_url`) and `local` otherwise. The `api` backend sends `STONKS_API_TOKEN`, the same token `stonks serve` and `stonks mcp` use. It sends it only over https, to a loopback host, or to a host listed in `[scheduler].api_trusted_hosts`, such as the `api` service on a private Compose network. With `api`, the closed-day check places tickers on calendars by suffix only, because the lake's asset classes are out of reach: name crypto tickers `.CC`. The `report` job reads only the state DB, so it runs in the scheduler process on every backend. The scheduler's own records (`scheduled_runs` and friends) live in `state.sqlite`, which several processes can share in WAL mode.

The Compose service for the scheduler needs these settings:

```yaml
scheduler:
  image: ghcr.io/<owner>/stonks:<tag>
  command: ["python", "-m", "stonks.scheduling", "run"]
  environment:
    STONKS_API_URL: http://api:8000
    STONKS_API_TOKEN: ${STONKS_API_TOKEN}      # the token the api service uses
    STONKS_DATA_DIR: /data                      # shares state.sqlite and scheduler.lock
    STONKS_NOTIFY_WEBHOOK_URL: ${STONKS_NOTIFY_WEBHOOK_URL}
    STONKS_PING_TICK: ${STONKS_PING_TICK}       # optional dead-man URLs
    STONKS_PING_INGEST: ${STONKS_PING_INGEST}
  volumes: ["data:/data"]
  depends_on: [api]
  stop_grace_period: 2m                         # let a running job finish
```

Also set `[scheduler] api_trusted_hosts = ["api"]` in the mounted config, since `http://api:8000` is neither https nor loopback. Run exactly one scheduler. With the `api` backend, don't also start the in-process one inside `stonks serve`. If both try, the second one finds the lock taken and doesn't start.

If the scheduler stops while it waits for an API job, the job keeps running in the API. The run is recorded as `failed` with the job id, so check `GET /api/jobs/{id}` before rerunning it.

### Market calendars

Exchange sessions come from the `exchange_calendars` library, wrapped in `stonks.scheduling.calendar`. They cover holidays, early closes and the exact UTC open and close of every session, so DST is handled for you. The NYSE close is 21:00 UTC in winter and 20:00 UTC in summer, and a job set to "30 minutes after the close" follows it. The day after Thanksgiving the NYSE closes at 13:00 New York time, and the job runs 30 minutes after that.

Tickers are placed on a calendar by their exchange suffix: `.US` uses NYSE (`XNYS`), `.LSE` uses London (`XLON`), `.XETRA` uses Xetra (`XETR`), and so on for every exchange Stonks ingests. Crypto (`.CC`, or any instrument whose asset class is `crypto`) trades 24/7. Commodities (`.COMM`) use the CME calendar. Government bonds (`.GBOND`) use a 24/5 calendar.

The tick and price ingest skip a day on which no instrument in the universe trades. An equity-only US universe therefore skips weekends and NYSE holidays, and a universe with any crypto in it never skips. A ticker that can't be placed on a calendar never causes a skip. Set `params = { skip_closed_days = false }` on a job to turn the check off.

### Jobs

The default jobs follow the NYSE close:

| Job | Action | When | Deadline |
|-----|--------|------|----------|
| `ingest_prices` | Pulls the last 7 days of daily bars for `[production].universe` | NYSE close + 30 min, trading days | 60 min |
| `tick` | Runs the production tick for that session's date | NYSE close + 45 min, trading days | 60 min |
| `report` | Writes the HTML report to `reports/latest.html` next to the state DB | NYSE close + 90 min, trading days | none |
| `health` | Runs the health checks and alerts when unhealthy | every 4 hours | none |

Set your own in `config/default.toml`. Listing any `[[scheduler.jobs]]` replaces the whole default list:

```toml
[scheduler]
catch_up = "latest"          # none | latest | all
max_catch_up_runs = 3        # cap for catch_up = "all"
catch_up_window_hours = 72   # older misses are never caught up
poll_seconds = 60            # longest sleep between wake-ups
watchdog_seconds = 60        # heartbeat and deadline check interval
# lock_path = "data/scheduler.lock"   # default: next to the state DB

[[scheduler.jobs]]
name = "ingest_prices"
action = "ingest_prices"
params = { lookback_days = 7, source = "eodhd" }   # tickers = [...] overrides the universe
deadline_minutes = 60
ping_url_env = "STONKS_PING_INGEST"
trigger = { type = "session", calendar = "XNYS", anchor = "close", offset_minutes = 30 }

[[scheduler.jobs]]
name = "tick"
action = "tick"
deadline_minutes = 60
ping_url_env = "STONKS_PING_TICK"
trigger = { type = "session", calendar = "XNYS", anchor = "close", offset_minutes = 45 }

[[scheduler.jobs]]
name = "crypto_tick"
action = "tick"
params = { tickers = ["BTC-USD.CC", "ETH-USD.CC"] }
trigger = { type = "daily", at = "00:30", timezone = "UTC" }

[[scheduler.jobs]]
name = "health"
action = "health"
trigger = { type = "interval", every_minutes = 240 }
```

Trigger types:

- `session`: `offset_minutes` after (or, if negative, before) a calendar's `open` or `close`, on its trading days only. The run is for that session's date, even when the offset carries it past midnight UTC.
- `daily`: a wall-clock time `at` in an IANA `timezone`, optionally only on `weekdays` (0 = Monday) or on a `calendar`'s trading days. The run is for that local date. A time that doesn't exist on the spring-forward day runs an hour later in local terms. A time that happens twice on the autumn day runs at its first occurrence. Either way it runs once.
- `interval`: every `every_minutes`, on a fixed grid counted from midnight UTC, so restarts never shift it.

A `daily` tick is for its local date, so a crypto tick at 00:30 UTC trades the day that has just started, as `stonks tick` does. Put the tick after the ingest it depends on: jobs due at the same time run in fire order, one at a time.

### Runs, catch-up and double runs

Every fire becomes one row in `scheduled_runs` (state migration 010). The row is keyed by job and run key: the session or local date for `session` and `daily` triggers, the UTC instant for `interval` triggers. A fire runs only if its row could be inserted. So a fire never runs twice, whether the scheduler restarts, catches up, or a second scheduler starts by mistake. Changing a job's offset doesn't rerun a day that already ran. A second `run` also refuses to start: the first one holds an OS lock on `scheduler.lock` (exit code 2).

When the scheduler starts, it looks at each job's fires since its last recorded run, within `catch_up_window_hours`:

- `none` drops them.
- `latest` runs only the most recent one. This is the default. It suits the tick, which refuses to trade a date older than its latest snapshot anyway.
- `all` runs up to `max_catch_up_runs` of the most recent, oldest first.

A job that has never run has nothing to catch up, so a fresh install does not replay yesterday. While the scheduler is running, a job with several fires due at one wake-up (the machine slept, or a long job held the loop) runs once, for the latest fire. Catch-up runs are flagged `catch_up = 1`. `run-now` runs are keyed `manual:<time>` and don't count as the job's last run.

A failed run is not retried automatically. It alerts, and you rerun it with `run-now`. When a job raises, the run is recorded as `failed` and an error alert goes through `[notify]`. The tick and health jobs send their own alerts (see [Alerts](#alerts)), so the scheduler doesn't send a second one. On shutdown (Ctrl+C or SIGTERM) the running job finishes, nothing new starts, and the process exits. A job interrupted by a crash is marked `failed` ("interrupted") at the next start.

### Dead-man's switch

Two checks make sure the jobs actually ran:

- **Deadlines.** For each job with `deadline_minutes`, a watchdog thread checks every `watchdog_seconds` that the latest fire whose deadline has passed has a `succeeded` or `skipped` run. If it doesn't (the job never started, is still running, or failed), the watchdog sends one error alert per missed run through `[notify]`. Alerts are recorded in `scheduler_deadline_alerts`, so a restart doesn't repeat them. Fires from before the scheduler first started are ignored. `python -m stonks.scheduling check` runs the same check once, for use from an external timer.
- **External pings.** Give a job a healthchecks.io-style URL and it POSTs `<url>/start` when it starts, then `<url>` on success or `<url>/fail` on failure, with the run id as `rid`. Set the monitor's schedule and grace time to match the job. The monitor then alerts when the pings stop, which also covers the scheduler process or the whole machine dying. Keep URLs in `.env`, since they embed the check's token: `ping_url_env = "STONKS_PING_TICK"` reads `STONKS_PING_TICK`. Logs only show `scheme://host/***`. A monitor that is down never fails the job.

### Metrics and probes

`stonks.scheduling.metrics` renders Prometheus text format (`text/plain; version=0.0.4`) from the state DB:

| Metric | Type | Meaning |
|--------|------|---------|
| `stonks_tick_runs_total{status}` | counter | Ticks by status. |
| `stonks_tick_duration_seconds` | gauge | Duration of the latest finished tick. |
| `stonks_tick_last_success_timestamp_seconds` | gauge | When the latest `ok` or `partial` tick finished. |
| `stonks_orders_total{status}` | counter | Orders by status. |
| `stonks_order_rejections_total` | counter | Orders the broker rejected. |
| `stonks_data_age_tickers{bucket}` | gauge | Universe tickers by age of their latest daily bar: `0-1d`, `2-3d`, `4-7d`, `8d+`, `missing` (with `--data-age`). |
| `stonks_data_oldest_bar_age_days` | gauge | Age of the stalest latest bar (with `--data-age`). |
| `stonks_jobs{status}` | gauge | API background jobs by status. |
| `stonks_job_queue_depth` | gauge | Queued background jobs plus scheduled runs in progress. |
| `stonks_scheduled_job_last_success_timestamp_seconds{job}` | gauge | When each scheduled job last succeeded. |
| `stonks_scheduled_job_last_status{job,status}` | gauge | 1 for the status of each job's latest run. |
| `stonks_scheduled_job_next_run_timestamp_seconds{job}` | gauge | Each job's next fire. |
| `stonks_scheduler_heartbeat_timestamp_seconds` | gauge | The running scheduler's latest heartbeat. |

`--data-age` opens the lake, so use it only while no `stonks serve` holds it. The API can serve the full set, data age included, from its own process with `metrics_text(state_path, specs=..., latest_bars=latest_daily_bars(lake, universe))`.

A useful alert rule: `time() - stonks_scheduled_job_last_success_timestamp_seconds{job="tick"} > 26 * 3600` on weekdays.

The probes are `liveness()` (the process answers), `readiness(state_path, lake_path)` (the state DB opens with every migration applied, and the lake file exists) and `scheduler_liveness(store, now=...)` (the scheduler hasn't stopped and has heartbeated recently). The watchdog thread writes the heartbeat, so it keeps beating while a long job runs.

### Without the scheduler

The three commands still work on their own. If you'd rather keep cron, run them after the close in UTC terms. For US equities, 22:30 UTC is after the close in both summer and winter, and leaves the vendor time to publish the day's bar.

```cron
30 22 * * 1-5  cd /opt/stonks && uv run stonks ingest prices --tickers AAPL.US,MSFT.US >> logs/ingest.log 2>&1
45 22 * * 1-5  cd /opt/stonks && uv run stonks tick >> logs/tick.log 2>&1
0  23 * * 1-5  cd /opt/stonks && uv run stonks health --notify >> logs/health.log 2>&1
```

cron doesn't know about exchange holidays. On those days the tick finds no new bars and trades nothing. The built-in scheduler skips those days instead.

## Alerts

Alerts go through the `Notifier` seam in `src/stonks/notify/`. It is configured under `[notify]` in `config/default.toml`:

```toml
[notify]
backends = ["log", "store", "webhook"]   # [] disables alerts; default ["log", "store"]
min_level = "warning"           # info | warning | error

[notify.webhook]
timeout_seconds = 5.0
```

Put the webhook URL in `.env`, not in TOML. Chat webhooks embed their token in the URL.

```dotenv
STONKS_NOTIFY_WEBHOOK_URL=https://hooks.slack.com/services/...
```

Backends:

- **log** writes a `notify` event to the structured log at the alert's level.
- **store** records every alert, whatever `min_level` says, in the state DB's `alerts` table, which the trader console reads through `GET /api/alerts`. Configured credentials (API token, broker keys, EODHD key, webhook URL), `key=value` credential pairs and `Bearer ...` values are scrubbed from the title, message and context before the row is written, and context entries whose key names a credential (`token`, `api_key`, `password`, ...) are replaced with `***`.
- **webhook** POSTs JSON: `{"level", "title", "message", "fields", "text"}`. `text` is a one-line summary, so Slack and Mattermost incoming webhooks show it as is. Other receivers can read the structured fields. The URL is never logged: error messages show only `scheme://host/***`.

A notifier never raises. A down webhook is logged as `notify.webhook.failed` and the tick or health check carries on with its own result.

What triggers an alert:

| Source | Level | When |
|--------|-------|------|
| `stonks tick` | error | The tick raised. The tick is recorded as `error` in `tick_runs` and the command exits non-zero. |
| `stonks tick` | warning | Status `partial`: at least one order raised at the broker. |
| `stonks tick` | warning | The broker rejected one or more orders. `fields.rejected` lists the tickers. |
| `stonks health --notify` | error | Any check failed. `fields.failed_checks` lists them. |

Orders the risk layer clips or drops are not alerts. They are listed under `risk_adjustments` in the tick's `summary_json` and logged as `risk.adjusted` events with the rule and reason.

## Health checks

`stonks health` exits 0 when healthy and 1 otherwise. Thresholds live under `[production.health]`:

| Check | Fails when | Setting |
|-------|------------|---------|
| `freshness:<ticker>` | The latest daily bar is older than N calendar days, or there are none. | `max_bar_age_days` (4) |
| `stuck_ticks` | A `tick_runs` row has been `running` for more than N minutes. | `stuck_tick_minutes` (60) |
| `stuck_ingest_runs` | An `ingest_runs` row has been `running` for more than N minutes. | `stuck_ingest_minutes` (180) |
| `ingest_failures` | An ingest run with status `error` started in the last N hours. | `ingest_failure_lookback_hours` (24) |

The ticker list is `--tickers`, or `[production].universe` when that flag is omitted. A check that crashes, for example on a missing table, counts as failed and does not stop the command.

## Risk policy

The risk layer sits between `strategy.decide` and the broker. It is configured under `[production.risk]`:

| Setting | Effect |
|---------|--------|
| `enabled` | `false` passes orders through untouched. |
| `max_open_positions` | A buy that would open a new position beyond this count is dropped. Adding to an existing position is allowed. |
| `max_weight_per_ticker` | Buys are clipped so the position stays within this fraction of portfolio value. |
| `max_weight_per_asset_class` | A table such as `crypto = 0.2`. Buys are clipped per class. While any class cap is set, buys of tickers with an unknown class are dropped. Buys in a capped class are also dropped while that class holds a position with no current price, since its exposure can't be measured. |
| `cash_buffer_fraction` | Buys are clipped so this fraction of portfolio value stays in cash, net of slippage and fees. |
| `min_order_notional` | Buys whose final notional is below this are dropped. |

Weights are measured against portfolio value (cash plus positions at current prices) before the tick's orders. Sells are never blocked. They are only clipped to the held quantity, so a sell cannot open a short. They are placed before buys, and buys are then checked again against the portfolio as it stands after the sells. A sell that is rejected or fails at the broker therefore never funds a buy. Shadow strategies go through the same policy.

## Shadow mode

Strategies with registry status `shadow` are ranked every tick, separately from active ones, and never traded. Each one runs as if it were the only active strategy, against its own virtual portfolio seeded with `initial_cash`:

- `shadow_decisions` has one row per hypothetical order: `tick_id`, `strategy_id`, `as_of`, `ticker`, `side`, `quantity`, `price` (the simulated fill price), and `status` (`filled` or `rejected`).
- `shadow_portfolio_snapshots` has one row per strategy per `as_of`, with the virtual cash, positions and marked value.

A shadow strategy with no picks for the day holds, and its portfolio is still marked to market. A failing shadow strategy is reported as `failed` in the tick summary's `shadow` list and never affects the real tick. Turn the feature off with `[production].shadow_enabled = false`. `--dry-run` writes no shadow rows.

To compare a shadow strategy before promoting it:

```bash
uv run stonks pnl --strategy <shadow-id>
uv run stonks registry promote <shadow-id>
```

## Reading P&L

```bash
uv run stonks pnl                      # the real portfolio, from inception
uv run stonks pnl --since 2026-09-01   # show only rows from this day on
uv run stonks pnl --strategy <id>      # a shadow strategy's virtual portfolio
```

The table has one row per day:

| Column | Meaning |
|--------|---------|
| `date` | UTC day. For the real portfolio this is the last snapshot taken that day, so a same-day rerun replaces the earlier run. For a shadow strategy it is the `as_of`. |
| `value` | Cash plus positions, marked at that tick's prices. |
| `change` | Value change against the previous row. |
| `daily` | That change as a return. |
| `cumulative` | Return since the first snapshot ever taken. |
| `drawdown` | Distance below the running peak value: 0% at a new high, negative otherwise. |

`--since` only trims the rows shown. `cumulative` and `drawdown` are still measured from inception, so a row reads the same whatever window you ask for. `--dry-run` ticks write no snapshots and do not appear here.
