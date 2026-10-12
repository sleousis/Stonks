# Full backfill from EODHD

`stonks ingest backfill` fills the lake with everything an EODHD All-in-one plan offers that Stonks has tables for. It covers every exchange and every symbol, delisted ones too. Free sources (SEC EDGAR, IBKR's short stock files, DefiLlama) run as phases too, outside the EODHD budget. That is far more than one day of API calls, so the backfill runs for days. It keeps its place in the state database, stays inside the daily call budget and starts again where it stopped.

```mermaid
flowchart LR
  P[Phases in order] --> U[Units: an exchange, a ticker, a calendar, a pair]
  U --> B{Budget left today?}
  B -->|yes| F[Fetch on at most 2 threads]
  F --> W[Write through the ingest pipeline]
  W --> S[(backfill_units in state.sqlite)]
  B -->|no| Z[Sleep to midnight UTC, or exit with --until-budget]
  Z --> B
```

## Before the first run

1. Back up `data/lake.duckdb` and `data/state.sqlite` (`stonks backup backup`).
2. Run `uv run stonks db init`. The backfill needs the lake's `instruments` table, lake migration 026 and state migration 061. An install that ran an earlier backfill must run it again before the new phases.
3. For a big lake, move bars to Parquet first: `uv run python -m stonks.store.bars_migrate --to parquet`.
4. Tell Stonks the plan. The shipped default is the free plan, which the backfill refuses. Set it in your own config or the environment, never in the shipped `config/default.toml`:

```toml
[ensure.plans]
eodhd = "all_in_one"
```

or `STONKS_EODHD_PLAN=all_in_one` in `.env`. The ensurer then also serves full history, bulk calls and intraday bars.

5. Stop `stonks serve`. DuckDB lets one process write the lake.

## Running it

```bash
uv run stonks ingest backfill run                    # runs for days, sleeps through each reset
uv run stonks ingest backfill run --until-budget     # exits when today's budget is spent
uv run stonks ingest backfill run --phases daily,actions --exchanges US,LSE
uv run stonks ingest backfill run --max-calls 5000   # a short trial
uv run stonks ingest backfill status [--json]
uv run stonks ingest backfill retry [--phases daily] # failed units get new attempts
```

Use `--until-budget` when a scheduler starts the run each day. Without it the process sleeps until 10 minutes after midnight UTC and goes on. Start a long run hidden and at low priority, for example on Windows:

```powershell
Start-Process -WindowStyle Hidden -FilePath uv -ArgumentList "run","stonks","ingest","backfill","run" -PassThru |
  ForEach-Object { $_.PriorityClass = "BelowNormal" }
```

`--exchanges` limits ticker phases to those exchange codes. Market-wide phases (calendars, macro, indices, Treasury, borrow, TVL) still run.

## Phases

Phases run in this order. Each is one module in `src/stonks/ingest/backfill/phases/`, so a new phase is one new module.

| Phase | Units | What it stores | Calls per unit |
|---|---|---|---|
| `symbols` | each exchange | `instruments`: asset class, equity kind, ISIN, delisted flag | 2 |
| `daily` | each instrument | full daily history in `bars` | 1 |
| `daily_topup` | each exchange and weekday | recent days by one bulk call, for tickers fetched before that day | 100 |
| `actions` | each equity | `dividends`, `stock_splits` | 2 |
| `fundamentals` | each common stock, ETF and fund | profile, analysts, holders, officers, shares outstanding, ESG, statements with versions (stocks), fund holdings (ETFs, funds), market cap (active stocks), insider trades (only on `insider_exchanges`) | 10 to 30 |
| `index_constituents` | each index on `INDX` (about 1,700) | `index_constituent_snapshots` and `index_constituent_changes`: members today and every past member with its dates, delisted names too | 10 |
| `exchange_details` | each exchange | `exchange_details` (hours, time zone, MICs, with versions) and `exchange_holidays` (from `exchange_holidays_start` to a few years ahead, early closes too) | 1 |
| `treasury` | each Treasury curve and year from `treasury_start_year`, the current year once a month | `treasury_rates` with versions: bill rates, nominal and real par yield curves, long-term rates | 1 |
| `calendar_extra` | IPOs, splits | `ipo_calendar` and `split_calendar` with versions, from `calendar_start`, a month a step | 1 a month |
| `id_mapping` | each exchange with instruments | ISIN, CUSIP, CIK, FIGI and LEI on `instruments`, a page of 1,000 a step (a NULL never clears a value) | 1 a page |
| `calendars` | earnings, dividends, economic | the three calendars from `calendar_start`, a month a step | about 1 to 6 a month |
| `macro` | each country and indicator | `macro_indicators` | 10 |
| `fx` | each six-letter pair on FOREX | `fx_rates` | 1 |
| `news` | each active stock and ETF | `news`, `news_sentiment` | 10 |
| `intraday_1h`, `intraday_5m`, `intraday_1m` | each common stock and ETF on `intraday_exchanges` | `bars` at that interval, a request window a step | 5 a step |
| `edgar` | each active US stock, each manager in `edgar_filers` | `corporate_filings` (every form), insider trades from Form 4, `institutional_holdings` from 13F, each with its acceptance time | free |
| `borrow` | each IBKR short stock market, each day | `borrow_rates` (the files hold today only) | free |
| `tvl` | each DefiLlama chain, each day | `defi_tvl`, the whole series each time | free |

Within a phase, stocks run first, then ETFs, then funds, and active names before delisted ones. Large indices (S&P 500, Nasdaq 100, FTSE, DAX and others) and the US exchange come first in their phases. Forex pairs and money market rates are not instruments (`skip_exchanges`). One `/fundamentals` call fills the profile, the statements and the holdings together, so a stock never pays for it twice.

## Free sources

`edgar`, `borrow` and `tvl` read free sources with their own rate limits (EDGAR at most 8 requests a second). They spend no EODHD calls. They run in order after the intraday phases, and also whenever the day's budget is used up: then they keep going until the reset and give way to the EODHD phases again.

`edgar` skips with a message until `[sources.edgar] user_agent` (or `STONKS_EDGAR_USER_AGENT`) holds a name and a contact address, as the SEC asks. See [edgar.md](edgar.md). A ticker EDGAR does not list is done with nothing stored. `borrow` and `tvl` list one unit per market or chain each day. A unit from a day that passed is done without a fetch, since today's unit reads the same file or series.

## Budget

Costs are EODHD's, measured on an All-in-one account:

| Endpoint | Calls |
|---|---|
| `/eod`, `/div`, `/splits`, symbol lists, calendars (IPOs and splits too), economic events | 1 |
| `/exchange-details`, `/ust/*` (each Treasury year), each `/id-mapping` page | 1 |
| `/intraday`, `/news`, `/sentiments` | 5 |
| `/fundamentals` (index members too), `/historical-market-cap`, `/insider-transactions`, `/macro-indicator` | 10 |
| `/eod-bulk-last-day` | 100 |
| `/user` | 0 |

The new reference endpoints were measured on 2026-10-12 against the account's own count while another run shared the key. Index members cost 10 each time, and the others came out at about one call a request. EODHD's own docs also say one for the Treasury endpoints.

Before each request the budget:

- paces requests to `requests_per_minute` (900, EODHD allows 1000),
- reads the account's count from `/user` at the start and every 10 minutes, so other processes on the same key count too,
- refuses a request that would pass `daily_budget` (95000) or the plan's own limit.

A refused request never leaves. The unit stays as it was and the next run picks it up. An HTTP 402 (allowance used) stops the day the same way. An HTTP 429 (too fast) is retried by the source after `Retry-After`. If the vendor still refuses, the unit is left as it was with no attempt counted, and the run pauses a minute, then twice as long for each refusal in a row, up to 15 minutes. The day starts again at midnight UTC, when EODHD resets its counter.

The next batch is fetched while the last one is stored, so it leaves room for the calls the batch in flight may still spend. A unit is never cut off half way by the budget.

## Failures

A unit that fails is tried again later, waiting 30 minutes and then twice as long each time. After 4 attempts it is marked failed. `retry` gives failed units new attempts. A ticker the vendor does not know (HTTP 404) is done with nothing stored and never asked again. A phase whose listing fails (the exchange list is down, say) is skipped for this pass and listed again on the next run. A write that raises is tried again one unit at a time, so only the unit that cannot be stored fails. The calls spent are stored however the run ends, Ctrl+C included. Every write goes through the ingest pipeline, so bars get the usual quality checks and quarantine, and each batch has an `ingest_runs` row.

## Status

`status` reads only the state database. Per phase it shows units done, failed, waiting to retry and left, the calls spent, and the calls the rest should take at the average cost of the done units. It also shows today's calls and the days left at the daily budget.

## Settings

Under `[ingest.backfill]`, all optional:

| Key | Default | Meaning |
|---|---|---|
| `daily_budget` | 95000 | calls a UTC day may reach |
| `requests_per_minute` | 900 | request pace |
| `network_threads` | 2 | requests in flight, at most 2 |
| `max_attempts` | 4 | tries before a unit fails |
| `retry_backoff_minutes` | 30 | first wait before a retry |
| `usage_check_seconds` | 600 | how often `/user` is read |
| `reset_margin_minutes` | 10 | wait after midnight UTC |
| `duckdb_memory_limit`, `duckdb_threads` | `1GB`, 2 | DuckDB limits during the run |
| `skip_exchanges` | `FOREX`, `MONEY` | exchanges whose symbols are not instruments |
| `extra_exchanges` | none | codes to list besides the exchange list |
| `symbols_relist_days` | 7 | how often symbol lists are read again |
| `topup_days` | 10 | how far the bulk top-up looks back |
| `market_cap_delisted` | false | market cap for delisted stocks too |
| `insider_exchanges` | none | exchanges whose stocks get insider trades. Off because EODHD answered empty lists for AAPL on 2026-10-12. `stonks ingest edgar` has US insider trades |
| `calendar_start` | 2000-01-01 | first day of the calendars |
| `macro_countries`, `macro_indicators` | all | which macro series |
| `news_limit` | 1000 | articles per news request |
| `intraday_exchanges` | `US` | exchanges with intraday bars |
| `intraday_delisted` | false | intraday bars for delisted names too |
| `intraday_start` | 1h and 5m 2020-10-01, 1m 2004-01-01 | first day per interval |
| `intraday_window_days` | 1h 7200, 5m 600, 1m 120 | days per intraday request |
| `index_codes` | all | index codes to fetch, e.g. `GSPC` |
| `exchange_holidays_start`, `exchange_holidays_years_ahead` | 2000-01-01, 2 | the holiday window |
| `treasury_start_year` | 1990 | first Treasury year (a year before a curve starts costs one call and stores nothing) |
| `id_exchanges` | all with instruments | exchanges for identifiers |
| `edgar_kinds` | `filings`, `insiders` | what EDGAR fetches per stock |
| `edgar_filers` | none | 13F managers by CIK |
| `borrow_markets` | all | IBKR short stock markets, e.g. `usa` |
| `tvl_chains` | all | DefiLlama chains |

## Tables

Lake migration 026 adds `exchange_details`, `exchange_holidays`, `treasury_rates`, `ipo_calendar` and `split_calendar`. All but the holidays keep a `_versions` twin with `known_at`, so a revised value never shows at a decision made before Stonks saw it (P12). Index members use the tables of migration 016 and identifiers the `instruments` columns.

The Treasury phase reads each past year once and the current year once a month, so its newest days arrive monthly. The IPO and split calendars, like the other calendars, stop at the day they reach.

State migration 061 adds `backfill_units` (one row per unit with status, attempts, calls, rows, cursor and last error), `backfill_phases` (when each phase last listed its units) and `backfill_calls` (calls per UTC day and the vendor's own count).
