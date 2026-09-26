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
| `rule` | Filters checked on the lake at each rebalance date | `start`, `end`, `rebalance`, `min_adv`, `min_price`, `asset_classes`, `sectors`, `exclude_sectors`, `exchanges` |
| `index` | Index members rebuilt from a change history | `index_id`, `source`, `start_date` |

A plain `list` has survivorship bias unless its spans say when names joined and left. The lab preflight warns about it.

## Refresh

A refresh turns a definition into rows of `universe_membership` (migration 015). A ticker is a member from `start_date` up to the day before `end_date`. The refresh replaces the universe's rows each time, so running it twice changes nothing.

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
3. It fetches the gaps on a thread pool, with one rate limiter per source.
4. On a paid plan, an exchange that misses the same few days is fetched with one bulk call a day. If that fails it falls back to one call per ticker.
5. One thread writes everything through the ingest pipeline, with quality checks and one `ingest_runs` row. A failing ticker is logged and skipped.

## Where it is used

- **Lab**: give `LabDataset` a `universe_id` and no tickers. The run takes every member over the window. With a `data_ensurer`, `LabRunner` fetches the missing bars before the preflight.
- **Tick**: `production.universe.resolve_tick_universe` accepts a list or a universe id.
- **API**: `/api/universes` lists, shows, creates and deletes. `/members?as_of=` gives members on a day. `POST /{id}/refresh` and `POST /{id}/ensure` run as background jobs, because DuckDB has one writer. `POST /index-history` imports a history.
- **MCP**: `list_universes`, `get_universe`, `get_universe_members`, and the guarded `create_universe`, `refresh_universe`, `ensure_universe_data` and `import_index_history`, which need `confirm=true`.

## Adding a kind or an index source

A new kind is one module in `universes/providers/` with a `UniverseProvider` subclass. A new index adapter is one module in `universes/index_sources/` with an `IndexSource` subclass. Both registries find them. Vendor parsing stays in the adapter.
