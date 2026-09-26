# Runbook: data stale or bad

Use this when `stonks health` reports stale bars, an ingest alert arrives ("Ingest data quality: ..."), or a strategy acts on a price that looks wrong.

## How ingest guards the bars

Every `ingest prices` and `ingest intraday` batch is checked before it reaches the bar store.

Rows that cannot be right are **quarantined**: written to the lake table `quarantined_bars` with reason codes, and kept out of `bars`.

| Reason | Meaning |
|--------|---------|
| `missing_price` | Open, high, low or close is empty. |
| `non_positive_price` | A price is zero or negative. Allowed for bonds (yields) and commodities (futures), where it can be real. |
| `high_below_low` | High is below low. |
| `close_out_of_range` | Close is outside [low, high], beyond a small rounding allowance. |
| `duplicate_timestamp` | The batch repeats a timestamp. The last copy is kept, as the store would. |
| `price_spike` | A one-bar jump beyond 10 robust sigmas (and at least 25%) that the next bar takes back almost fully. Equity and crypto only. |

Findings that can be real are **warnings**: reported and alerted on, never dropped.

| Warning | Meaning |
|---------|---------|
| `extreme_move` | A large move that did not revert: a crash, a squeeze, or a split with no split record. |
| `stale_series` | The newest daily bar is more than 7 days before the requested end date. |
| `flat_price_streak` | 10 or more unchanged closes in a row. |
| `zero_volume_streak` | 5 or more zero-volume bars in a row. |
| `calendar_gap` | Sessions missing against the market calendar (only when a calendar is wired in). |

Splits are checked against the lake's `stock_splits` (and the vendor's adjusted close) before a move counts, so a 4:1 split is neither a spike nor a warning. Ingest `metadata` for the ticker to record its splits.

Each run stores its summary in `ingest_runs.quality_json` (bars checked and quarantined, counts by reason and warning, and which tickers a fallback source supplied). An alert goes out through the notifier when a run quarantines a bar, when 5 or more tickers carry warnings, or when a fallback source was used. Thresholds live under `[ingest.quality]`.

## Fallback source

With `[ingest.fallback] sources = { eodhd = "yahoo" }`, a ticker whose fetch fails on EODHD is retried on Yahoo. The bars look the same (the schema is vendor-agnostic); the run's `quality_json.supplied_by` records which ticker came from where. Fallback is off by default.

## Triage

1. Find the run:

   ```sql
   SELECT id, source, kind, status, tickers_ok, tickers_failed, error, quality_json
   FROM ingest_runs ORDER BY id DESC LIMIT 10;
   ```

   (`uv run duckdb -ui data/lake.duckdb`, or `DuckDBLake.sql` from Python.)

2. **Failed tickers** (`tickers_failed > 0`): read `error`. A vendor outage or rate limit clears on a rerun: `uv run stonks ingest prices --tickers <list> --since <date>`. If one vendor keeps failing, configure a fallback source and rerun.

3. **Quarantined rows**:

   ```sql
   SELECT ticker, timestamp, interval, open, high, low, close, reasons, source, run_id
   FROM quarantined_bars ORDER BY quarantined_at DESC LIMIT 50;
   ```

   Compare with a second source (the vendor's web page, or a Yahoo fetch). If the vendor has since fixed the bar, rerun the ingest for that day; the corrected bar passes and lands in `bars`. If the quarantined bar was real (very rare), lower the threshold that caught it in `[ingest.quality]` and rerun.

4. **Stale series**: check the ticker still trades (delisted, renamed, halted) and that the ingest job ran (`stonks health`, scheduler history). A delisted name should leave the universe.

5. **Extreme moves**: check the news and the ticker's splits. If a split is missing, run `uv run stonks ingest metadata --tickers <ticker>` so the split is recorded; strategies read split-adjusted prices.

6. After fixing, run `uv run stonks health --notify` to confirm.
