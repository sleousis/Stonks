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

Timestamps are UTC, and `tick` defaults `--as-of` to today's UTC date. Schedule the tick after the day's bars have landed in UTC terms. For US equities, 22:30 UTC is after the close and leaves the vendor time to publish the day's bar. Check your vendor's publication time and adjust.

## Scheduling with cron (Linux, macOS)

```cron
# m  h  dom mon dow  command
30 22 *   *   1-5  cd /opt/stonks && uv run stonks ingest prices --tickers AAPL.US,MSFT.US >> logs/ingest.log 2>&1
45 22 *   *   1-5  cd /opt/stonks && uv run stonks tick >> logs/tick.log 2>&1
0  23 *   *   1-5  cd /opt/stonks && uv run stonks health --notify >> logs/health.log 2>&1
```

Notes:

- cron's `PATH` is minimal. Use the absolute path to `uv` (`which uv`) if the job can't find it.
- Set `CRON_TZ=UTC` (cronie) or keep the host clock in UTC so the times above mean UTC.
- The commands load `.env` from the working directory, which is why each line starts with `cd`.
- Logs are structlog JSON lines, one event per line. `jq` reads them well.
- To also run health checks through the day, add a line such as `0 */4 * * * cd /opt/stonks && uv run stonks health --notify`.

## Scheduling with Windows Task Scheduler

Put the commands in a batch file so the working directory and log paths are fixed, for example `C:\stonks\daily.cmd`:

```bat
@echo off
cd /d C:\stonks
uv run stonks ingest prices --tickers AAPL.US,MSFT.US >> logs\ingest.log 2>&1
uv run stonks tick >> logs\tick.log 2>&1
uv run stonks health --notify >> logs\health.log 2>&1
```

Register it with `schtasks`. `/ST` is local time: pick the local equivalent of 22:30 UTC.

```bat
schtasks /Create /TN "Stonks\Daily" /TR "C:\stonks\daily.cmd" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 23:30 /F
```

To run health checks every 4 hours as a separate task:

```bat
schtasks /Create /TN "Stonks\Health" /TR "cmd /c cd /d C:\stonks && uv run stonks health --notify >> logs\health.log 2>&1" /SC HOURLY /MO 4 /F
```

Useful follow-ups:

- `schtasks /Run /TN "Stonks\Daily"` runs the task now, to test it.
- `schtasks /Query /TN "Stonks\Daily" /V /FO LIST` shows the last run time and result. A last result of `1` from the health step means unhealthy.
- By default the task runs only while you are logged on. To run it logged off, add `/RU <user> /RP` (it prompts for the password). The account needs access to the repo, `uv` and `.env`.

## Alerts

Alerts go through the `Notifier` seam in `src/stonks/notify/`. It is configured under `[notify]` in `config/default.toml`:

```toml
[notify]
backends = ["log", "webhook"]   # [] disables alerts
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
| `max_weight_per_asset_class` | A table such as `crypto = 0.2`. Buys are clipped per class. While any class cap is set, buys of tickers with an unknown class are dropped. |
| `cash_buffer_fraction` | Buys are clipped so this fraction of portfolio value stays in cash, net of slippage and fees. |
| `min_order_notional` | Buys whose final notional is below this are dropped. |

Weights are measured against portfolio value (cash plus positions at current prices) before the tick's orders. Sells are never blocked. They are only clipped to the held quantity, so a sell cannot open a short. They are placed before buys, so the cash they free up is really there when the buys go in. Shadow strategies go through the same policy.

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
