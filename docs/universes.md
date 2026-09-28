# Universes and on-demand data

A universe is the set of tickers a strategy may trade. Stonks stores each universe as a named object and keeps its members point in time. A backtest on a date sees the names that were members on that date, including names that later died (principle P14).

Data follows the universe. You no longer ingest a fixed ticker list up front. When a lab run or an operator needs a universe over a window, Stonks fetches only the bars that are missing.

```mermaid
flowchart LR
  D[Universe definition] -->|refresh| M[universe_membership spans]
  M -->|members over a window| E[DataEnsurer]
  E -->|missing ranges only| S[DataSource]
  S --> L[(Lake bars)]
  M --> LAB[Lab run]
  M --> TICK[Production tick]
  L --> LAB
```

## Kinds of universe

| Kind | What it holds | Spec |
|---|---|---|
| `list` | Fixed tickers, or dated spans | `tickers`, `spans`, `start_date`. Also CSV import. |
| `exchange` | Every symbol a source lists on an exchange, delisted ones too | `exchange`, `source`, `security_types`, `include_delisted`, `start_date` |
| `rule` | A screen checked on the lake at each rebalance date | `start`, `end`, `rebalance`, `min_adv`, `min_price`, `asset_classes`, `sectors`, `exclude_sectors`, `exchanges`, and the screen keys `universe_id`, `filters`, `sort_by`, `descending`, `limit` (see [Screener](#screener)) |
| `index` | Index members rebuilt from a change history | `index_id`, `source`, `start_date` |

A plain `list` has survivorship bias unless its spans say when names joined and left. The lab preflight warns about it.

## Refresh

A refresh turns a definition into rows of `universe_membership` (migration 015). A ticker is a member from `start_date` up to the day before `end_date`. The refresh replaces the universe's rows each time, so running it twice changes nothing. An edit changes only the definition. The members stay as they were until the next refresh, and the console marks the universe as changed since.

- **exchange**: a span starts at the listing date, else the first bar, else `start_date`. A delisted name ends at its delisting date, else the day after its last bar. A delisted name with no dates and no bars stays a member up to the refresh date, with a warning. Ensure its data and refresh again to tighten it. The listings also fill the `instruments` table.
- **rule**: the filters run on each rebalance date (weekly, monthly or quarterly). A name joins on the date it passes and leaves on the date it fails. Rules read only the lake, so ensure the candidates' data first.
- **index**: the history is a snapshot of members plus dated additions and removals. Stonks walks back from the snapshot. Import a history as CSV or JSON, or name an adapter such as `wikipedia_sp500`.

CSV for an index history:

```text
date,ticker,action
2024-01-02,AAPL.US,member
2023-06-01,NEWCO.US,add
2023-06-01,OLDCO.US,remove
```

## On-demand data

`DataEnsurer` (`ingest/ensure.py`) takes tickers, a window and an interval.

1. It clips the window to today and to the plan's limit. The EODHD free tier keeps one year of daily prices and has no intraday or bulk data, so the start moves and the report warns.
2. It subtracts what the lake already has and the ranges it already asked for (`bar_fetch_ranges`). A dead name with no data is not asked for again.
3. It drops gaps that hold no closed session: weekends, market holidays, and today before the close. The market calendar decides. A ticker with no known calendar counts every weekday.
4. It fetches the gaps on a thread pool, with one rate limiter per source.
5. On a paid plan, an exchange that misses the same few days is fetched with one bulk call a day. If that fails it falls back to one call per ticker. A name that misses more days than a bulk run covers, such as a dead name, is fetched on its own and the rest still use bulk.
6. One thread writes everything through the ingest pipeline, with quality checks and one `ingest_runs` row. A failing ticker is logged and skipped. When the pipeline has a fallback source, it asks the fallback only for that ticker's own gaps, plus the overlap below.

### Adjusted prices stay on one basis

Vendors send `adj_close` adjusted as of the day you fetch. Bars fetched on different days can sit on different bases, and a split then looks like a crash (P13). So:

- Each daily fetch starts 5 stored bars before the gap (`overlap_bars`). In bulk mode the last stored day is fetched too.
- If the vendor's `adj_close / close` on an overlapping bar differs from the stored one by more than `adjustment_tolerance` (0.05%), a split or dividend happened since the last fetch. The ticker's whole history is fetched again in the same run. The report lists it under `readjusted`.
- Bars older than the vendor's history limit (one year on the EODHD free tier) cannot be fetched again. Their `adj_close` is multiplied by the same factor.
- Every daily writer runs the same check, not only the ensurer. When `stonks ingest prices` or the scheduled ingest refetches the last few days and their basis moved, the stored bars older than the batch get their `adj_close` multiplied by the factor. The run's quality summary lists the ticker under `readjusted`.
- A fallback source is asked for the overlap too, so its rows get the same check.

### Unfinished daily bars are never stored

A vendor's bar for today holds the latest trade until the session closes. The ingest pipeline drops any daily bar whose session has not closed yet, for every ingest, not only the ensurer. The ensurer does not record that day as fetched, so it asks again after the close. A partial bar stored before this rule existed is overwritten by the overlap of the next fetch.

Intraday bars follow the same idea. The ensurer stores only bars that are complete (bar start plus its length is past now). A day counts as covered only when its last stored bar reaches the session close, and a session still open is never recorded as fetched. So an ensure run in the middle of a session fetches the rest of that day next time.

## Settings

`[ensure]` in `config/default.toml` sets how fetches run.

| Key | Default | Meaning |
|---|---|---|
| `max_workers` | 8 | Fetches at once |
| `requests_per_second` | `eodhd = 10`, `yahoo = 2` | Rate limit per source |
| `default_requests_per_second` | 5 | Rate limit for other sources |
| `plans` | `eodhd = "free"` | Data plan per source. It sets the history, intraday and bulk limits. |
| `bulk` | false | One bulk call per exchange and day, on plans that allow it |
| `bulk_max_days`, `bulk_min_tickers` | 5, 20 | When bulk is worth it |
| `prefetch_window` | 64 | Fetches kept ahead of the writer |
| `settle_days` | 14 | Empty answers this recent are asked again |

`[production].universe` is a ticker list or a universe id:

```toml
[production]
universe = "sp500"          # members on the tick's date
# universe = ["AAPL.US", "MSFT.US"]   # a fixed list works as before
```

## Commands

```bash
uv run stonks universe list
uv run stonks universe create mine --tickers AAPL.US,MSFT.US
uv run stonks universe create mine --csv mine.csv
uv run stonks universe create us_all --kind exchange --spec '{"exchange": "US"}'
uv run stonks universe refresh mine [--as-of 2026-01-02]
uv run stonks universe show mine
uv run stonks universe members mine --as-of 2026-01-02
uv run stonks universe update mine --tickers AAPL.US,MSFT.US,NVDA.US [--name ... --spec JSON]
uv run stonks universe history mine [--ticker AAPL]
uv run stonks universe ensure mine --start 2025-01-01 --end 2025-12-31 [--interval 1d --source eodhd]
uv run stonks universe import-index sp500 history.csv
uv run stonks universe delete mine --yes
uv run stonks lab run momentum --universe-id mine --ensure-data --start 2025-01-01 --end 2025-12-31
```

These commands open the lake. While `stonks serve` runs, use the API or the console.

## Where it is used

- **Lab**: `stonks lab run --universe-id` takes every member over the window. `--ensure-data` fetches the missing bars first (window plus the strategy's warm-up). Over the API, a lab run takes `universe_id` and `ensure_data`. The fetch runs as its own `lab_ensure` job on the lake writer lane before the lab job, and the result names it in `ensure_job_id`.
- **Tick**: with a universe id in `[production].universe`, the tick trades the members on its date. Explicit tickers (`--tickers`, the request's `tickers`) still win. A universe with no members stops the tick with a clear error.
- **Sweeps**: `POST /api/lab/sweeps` takes `universe` or `universe_id`.
- **Scheduler**: the default `universes_refresh` job runs 20 minutes after the NYSE close, before the ingest and the tick. It refreshes every stored universe, then fetches missing bars over the last `ensure_days` (default 10). It skips when no universe is stored. Params: `ensure_days`, `interval`, `source`, `ensure = false` to only refresh.
- **API**: `/api/universes` lists, shows, creates, edits (`PUT /{id}`) and deletes. `/members?as_of=` gives members on a day. `/history` gives the membership spans, latest change first, and `?ticker=` narrows them. `/exchanges` lists the exchanges our instruments name, for an exchange universe. `POST /{id}/refresh` and `POST /{id}/ensure` run as background jobs, because DuckDB has one writer. `POST /index-history` imports a history. Creating, editing, refreshing and importing need `lab.run`, deleting needs `strategy.promote`. The universe the tick trades is an admin setting, so creating it while it is missing, editing it, or importing the index it follows needs `strategy.promote` too.
- **MCP**: `list_universes`, `get_universe`, `get_universe_members`, `get_universe_history`, `list_universe_exchanges`, and the guarded `create_universe`, `update_universe`, `refresh_universe`, `ensure_universe_data`, `import_index_history` and `delete_universe`, which need `confirm=true`.
- **Console**: the Universes page (`/universes`, linked from Data, the screener and the lab forms) lists, creates and edits each kind, shows members on a date and the membership history, runs Refresh and Fetch missing data as jobs, and deletes with the id typed. A rule is built with the screener's filter builder. See `docs/ui.md`. `wait_for_job` returns the typed refresh and ensure results.

## Screener

A screen filters instruments on price and fundamental metrics, on the lake as it was on a date. It is built on the `rule` universe, so a screen you like becomes a universe for the lab.

```mermaid
flowchart LR
  R[Rule filters: listed, class, sector, exchange, price, volume] --> C[Candidates]
  U[Optional stored universe] --> C
  C --> M[Metrics on the date]
  M --> F[Metric filters, sort, top N]
  F --> O[Rows]
  F -->|save as universe| RU[rule universe: the screen at each rebalance]
```

A spec has the rule filters (`asset_classes`, `sectors`, `exclude_sectors`, `exchanges`, `min_price`, `min_adv`), an optional `universe_id` to start from its members, `filters` such as `{"metric": "pe_ratio", "min": 0, "max": 15}`, then `sort_by`, `descending`, `limit` and extra `columns` to show.

```json
{
  "universe_id": "sp500",
  "filters": [
    {"metric": "pe_ratio", "max": 15},
    {"metric": "return_12m", "min": 0}
  ],
  "sort_by": "dividend_yield",
  "limit": 20
}
```

### Metrics

| Group | Metrics |
|---|---|
| price | `price`, `dollar_volume_20d`, `return_1m`, `return_3m`, `return_6m`, `return_12m`, `volatility_3m`, `from_high_52w` |
| fundamental | `market_cap`, `pe_ratio`, `pb_ratio`, `ps_ratio`, `dividend_yield`, `net_margin`, `roe`, `debt_to_equity`, `revenue_growth` |

`GET /api/screener/metrics` lists them with units. A new metric is one `ScreenMetric` class in `screener/metrics/`.

Every value is point in time (P12). Returns use adjusted closes up to the date. A last bar more than 10 days old means no price. Flows (revenue, net income) sum the last four quarters filed before the date, else the last annual statement. A statement counts from the day after its filing date, and one with no filing date counts as known 90 days after its period end. Each period uses the version Stonks knew on the date, so a later restatement never leaks back. The balance sheet is the latest one filed. The market cap is the stored one near the date, else price times shares.

A ticker with no value for a metric fails that metric's filter and sorts last. A loss has no P/E. Negative equity has no P/B or ROE.

### Large screens

A screen over a whole exchange can have thousands of candidates. Three things keep it fast and safe.

```mermaid
flowchart LR
  S[POST /size] -->|over the cap| X[Stop: narrow the screen]
  S -->|above the job threshold| J[POST /jobs: background job with progress]
  S -->|small| R[POST /run]
  J --> Res[GET /jobs/ID/result]
  R --> C[(Result cache: spec and date)]
  J --> C
```

- **A candidate cap.** When the rule keeps more than `max_candidates` names, the screen stops before it reads any metric. The API answers 422 with the count, the cap and how to narrow it: a universe, asset classes, sectors, exchanges, `min_price` or `min_adv`.
- **A background job.** `POST /api/screener/size` counts the candidates and says `use_job` above `job_threshold`. `POST /api/screener/jobs` queues the `screen_run` job. Follow it at `/api/jobs/{id}` or its event stream, with one progress step per metric, and cancel it there. Read the rows at `GET /api/screener/jobs/{id}/result`. The console does this by itself above the threshold.
- **A short cache.** The same spec on the same date returns the stored result for `cache_seconds`, marked `cached: true`. A new ingest shows up once that time has passed.

Metrics come from a few set-based DuckDB queries over all candidates at once, never a loop per ticker. The point-in-time rules above do not change, and a test checks the batched values against the old per-ticker ones and against data that arrives after the date.

On a synthetic lake of 5,000 tickers with 300 days of bars, every metric took 5.3 seconds before and 0.6 seconds after. Rerun it with `uv run python -m tools.screener_bench`.

```toml
[screener]
max_candidates = 10000
job_threshold = 1000
cache_seconds = 300   # 0 turns the cache off
cache_entries = 32
```

### Saved screens and universes

Saved screens belong to one person, like watchlists. Another person's screen is a 404.

Save a screen as a universe in one of two modes:

- **rule** (default): a `rule` universe that runs the screen at each rebalance date from `start` (default a year ago). The lab sees who passed on each day, dead names too (P14).
- **snapshot**: today's matches as a fixed `list`. It carries survivorship bias, and the result warns about it.

Both queue the universe refresh, so members appear when the job ends. The saved universe links to its page on the Universes page, where you can edit it later.

### Commands, API and MCP

```bash
uv run stonks screener metrics
uv run stonks screener run --spec '{"sectors": ["Technology"], "sort_by": "return_12m", "limit": 20}' [--as-of YYYY-MM-DD]
uv run stonks screener save NAME --spec JSON | list | delete ID
uv run stonks screener run --screen ID
uv run stonks screener universe ID (--spec JSON | --screen ID) [--mode rule|snapshot] [--start ... --end ...] [--rebalance monthly]
```

- API: `GET /api/screener/metrics`, `POST /api/screener/run`, `POST /api/screener/size`, `POST /api/screener/jobs` with `GET /api/screener/jobs/{id}/result`, `/api/screener/screens` (list, create, get, update, delete) and `POST /api/screener/universes`.
- MCP: `list_screen_metrics`, `run_screen`, `list_screens`, `get_screen`, `create_screen`, `update_screen`, and the guarded `save_screen_as_universe` and `delete_screen`, which need `confirm=true`.

Running a screen, as a job too, needs `data.read`. Saving one needs `portfolio.manage`. Saving it as a universe needs `lab.run`.

### Screen alerts

A saved screen can run daily or weekly after the price update and notify you about names that newly match (roadmap 23.17). The first run only notes the matches. See [operations](operations.md#screen-alerts).

- API: `GET /api/screener/alerts`, `GET /api/screener/alerts/events`, `GET|PUT|DELETE /api/screener/screens/{id}/alert`, and `POST /api/screener/alerts/evaluate` for the operator.
- MCP: `list_screen_alerts`, `list_screen_alert_events`, `set_screen_alert` and the guarded `delete_screen_alert`.
- Changing an alert needs `notifications.manage`.

## Adding a kind or an index source

A new kind is one module in `universes/providers/` with a `UniverseProvider` subclass. A new index adapter is one module in `universes/index_sources/` with an `IndexSource` subclass. Both registries find them. Vendor parsing stays in the adapter.
