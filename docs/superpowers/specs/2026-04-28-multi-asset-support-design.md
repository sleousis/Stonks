# Multi-asset support — design

**Status:** approved · **Date:** 2026-04-28 · **Branch:** `feature/multi-asset-support`

The system is currently equity-only: schemas, lake tables, EODHD adapter, strategies, the ranker — every layer is shaped around stocks. This change adds **crypto, commodities, and bonds** as first-class asset classes alongside equities, with the architectural seams to plug in more later.

Scope locked at design time: option **B** from brainstorm.
- Seams + bars work for every class (already does, since `bars` is interval-aware and asset-agnostic).
- Add small **per-class profile tables** capturing the metadata that's actually load-bearing for non-equity strategies.
- Equity-specific tables (fundamentals, dividends, insiders, analysts, ESG, splits, …) stay equity-only — they just don't get rows for non-equity instruments.
- Backtester / Broker / Portfolio explicitly out of scope: the bar-driven engine already handles any class; quirks specific to crypto 24/7 sessions, futures contract roll, and bond accrued-interest are deferred until a strategy actually requires them.

## 1. Asset-class taxonomy

New closed `Literal` in `core.types`:
```python
AssetClass = Literal["equity", "crypto", "commodity", "bond"]
```
Reserved set; future additions (`forex`, `fund`, `index`) are non-breaking.

`AssetClass` and `TickerProfile.security_type` are orthogonal: `asset_class` is the class (which-asset-kind), `security_type` is the equity sub-kind (`common_stock`, `preferred_stock`, `adr`, `etf`, `fund`, `other`). For non-equity rows, `security_type` is `None`.

## 2. Storage

### Table rename: `tickers` → `instruments`

`tickers` was always the abstract instrument table — CUSIP / ISIN / OpenFigi / LEI live there, the comment block already calls the row "the abstract instrument". The name was equity-loaded, and now matters: a crypto row in a table called `tickers` reads wrong. One-time rename, all downstream column references stay named `ticker` (the access key).

### New column: `instruments.asset_class`

`VARCHAR NOT NULL` after backfill. Existing rows backfill to `'equity'`. Application boundary enforces the `AssetClass` literal via Pydantic.

### Equity-only tables: unchanged

`fundamentals`, `dividends`, `insider_transactions`, `news`, `news_sentiment`, `earnings_announcements`, `analyst_forecasts`, `analyst_ratings`, `institutional_holders`, `esg_snapshots`, `esg_activities`, `cross_listings`, `officers`, `ticker_snapshots`, `shares_outstanding`, `employee_count`, `segmentation`, `stock_splits`, `market_cap_history` — all keep the same shape and PK `(ticker, …)`. They simply have zero rows for crypto/bond/commodity tickers.

No FK enforcement against `instruments(id)` — preserves the existing pragma of soft, soft-failing ingest where a profile may arrive after its first price bar.

### New per-class profile tables

```sql
CREATE TABLE crypto_profiles (
    ticker              VARCHAR PRIMARY KEY,    -- e.g. 'BTC-USD.CC'
    base_symbol         VARCHAR,                -- e.g. 'BTC'
    quote_symbol        VARCHAR,                -- e.g. 'USD'
    blockchain          VARCHAR,                -- free-text (Bitcoin, Ethereum, …)
    consensus_type      VARCHAR,                -- normalized literal at adapter
    circulating_supply  DOUBLE,
    total_supply        DOUBLE,
    max_supply          DOUBLE,
    supply_snapshot_date DATE
);

CREATE TABLE bond_profiles (
    ticker            VARCHAR PRIMARY KEY,
    issuer_name       VARCHAR,
    issuer_kind       VARCHAR,    -- 'sovereign' | 'corporate' | 'municipal' | 'agency' | 'other'
    bond_kind         VARCHAR,    -- 'treasury' | 'corporate' | 'municipal' | 'zero_coupon' | 'other'
    coupon_rate       DOUBLE,     -- annual %
    coupon_frequency  INTEGER,    -- payments per year
    face_value        DOUBLE,
    currency          VARCHAR,
    issue_date        DATE,
    maturity_date     DATE,
    credit_rating     VARCHAR     -- free-text (S&P/Moody's notation differs)
);

CREATE TABLE bond_yield_history (
    ticker             VARCHAR NOT NULL,
    date               DATE    NOT NULL,
    yield_to_maturity  DOUBLE,
    clean_price        DOUBLE,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE commodity_contracts (
    ticker             VARCHAR PRIMARY KEY,
    underlying_symbol  VARCHAR,        -- e.g. 'GC' for gold futures
    contract_kind      VARCHAR,        -- 'spot' | 'continuous' | 'futures' | 'index' | 'other'
    contract_month     VARCHAR,        -- e.g. '2026-06' for specific futures
    expiry_date        DATE,
    contract_size      DOUBLE,
    contract_unit      VARCHAR         -- e.g. 'troy_ounce', 'barrel', 'metric_ton'
);
```

Conventions inherited from the merged PR: `Field(ge=…, le=…)` validators where applicable, normalized `Literal` values at the adapter boundary, vendor-specific keys not modelled.

### Migration

One file: `store/migrations_duckdb/007_multi_asset.sql`. Steps:
1. `ALTER TABLE tickers RENAME TO instruments`
2. `ALTER TABLE instruments ADD COLUMN asset_class VARCHAR`
3. `UPDATE instruments SET asset_class = 'equity' WHERE asset_class IS NULL`
4. *(Don't enforce NOT NULL at SQL layer — Pydantic enforces the literal at insert time. Backfill above ensures every existing row has a value.)*
5. `CREATE TABLE crypto_profiles / bond_profiles / bond_yield_history / commodity_contracts` (DDL above)

### `prices` view dependency

The `prices` view from migration 003 references `bars`, not `tickers`, so the rename has no impact there.

## 3. Schemas (`ingest/schemas.py`)

- `TickerProfile` gains `asset_class: AssetClass = "equity"` (default keeps every existing source compatible).
- New `FrozenRow` subclasses: `CryptoProfileRow`, `BondProfileRow`, `BondYieldRow`, `CommodityContractRow`.
- `__all__` extended.

`MetadataBundle` (`ingest/metadata_bundle.py`) gains four optional fields:
```python
crypto_profile: CryptoProfileRow | None = None
bond_profile: BondProfileRow | None = None
commodity_contract: CommodityContractRow | None = None
bond_yields: tuple[BondYieldRow, ...] = ()
```

## 4. DataSource ABC + ingest pipeline

### `DataSource` ABC: shape unchanged

Same `fetch_prices` / `fetch_fundamentals` / `fetch_metadata` / `fetch_intraday_bars` / `list_tickers` / `list_exchanges`. Per-class data flows through `fetch_metadata` via the extended `MetadataBundle`. No new abstract methods, no asset-class-specific subclasses. Vendors that only cover some asset classes simply leave the new bundle fields empty.

Non-equity adapters subclass `DataSourceError` (added in the merged PR) for vendor-class errors so the pipeline's narrow `except` catches them.

### `IngestPipeline._upsert_bundle`: 4 new calls

Append upserts for the four new bundle fields. Empty / `None` entries no-op via the existing `df.empty` short-circuits.

### `EodhdSource` extensions

- New helper `classify(ticker: str) -> AssetClass`: suffix-based — `.CC` → crypto, `.COMM` → commodity, `.GBOND` → bond, anything else → equity.
- `fetch_fundamentals` and equity-only metadata sub-fetches (dividends, insiders, analysts, ESG, splits, holders, officers, earnings, …) short-circuit to empty when `classify(ticker) != "equity"` — no HTTP request.
- `fetch_metadata` populates `crypto_profile` for `.CC` tickers using EODHD's General endpoint (it returns symbol/blockchain/supply for crypto).
- `fetch_metadata` for `.COMM` and `.GBOND` populates `commodity_contract` / `bond_profile` from whatever EODHD's General endpoint exposes; fields the vendor doesn't provide stay `None`. Free-tier coverage of these classes is thin — that's expected, fields stay null, soft-fail per ticker.
- Each new parser is unit-testable from a captured fixture (`parse_crypto_profile_response`, `parse_bond_profile_response`, `parse_commodity_contract_response`).

## 5. Strategy + ranking + CLI

### `BaseStrategy.applicable_asset_classes`

```python
class BaseStrategy:
    applicable_asset_classes: tuple[AssetClass, ...] = ("equity",)
```

Default keeps every existing strategy equity-only with zero code changes. A cross-class strategy declares e.g. `applicable_asset_classes = ("equity", "crypto")`.

### `Ranker.rank` filters universe

`Ranker.rank(as_of, …)` reads each instrument's `asset_class` from `instruments` and filters per-strategy: only instruments whose class is in `strategy.applicable_asset_classes` enter that strategy's evaluation.

Implementation: one extra join against `instruments` in the universe-discovery query, or a small in-memory filter using a `(ticker → asset_class)` map fetched once per tick.

### CLI

- `stonks ingest prices --tickers BTC-USD.CC` — works without changes (asset class inferred from suffix; equity-only fetches short-circuit).
- `stonks ingest metadata --tickers BTC-USD.CC` — same.
- `stonks registry list --asset-class crypto` — new optional filter; intersection with `--status` if both provided.
- `stonks tick --asset-class crypto` — new optional filter; restricts the universe to instruments of the given class for the tick.

## 6. Out of scope (called out so we don't accidentally drift)

- **Backtester sessions**: 24/7 crypto markets, futures contract roll, bond accrued-interest pricing. Today's daily bar engine works for all classes; quirk-aware behavior is deferred.
- **Broker** abstractions: live broker per asset class (e.g. crypto exchange API, bond OTC desk) — `SimulatedBroker` keeps working, real per-class brokers are a future change.
- **Asset-class-specific objectives / survival tests**: the existing four (OOS, period stability, perturbation, drift) are class-agnostic; no changes needed now.
- **Cross-asset portfolio risk**: no notion of currency translation, asset-class allocation limits, or correlation-aware sizing. Out of scope.
- **Continuous futures construction**: if/when commodity strategies need a continuous-front-month synthetic series, that's a separate ingestion path.

## 7. Testing

Following the project's TDD rule + hermetic-default rule:
- Unit tests for asset-class classifier (`tests/unit/test_eodhd_asset_class.py`).
- Unit tests for new parsers using captured EODHD fixtures (`tests/unit/test_eodhd_crypto_parsing.py`, etc.).
- Unit tests for `MetadataBundle` extension (`tests/unit/test_ingest_schemas.py` extension).
- Integration test for migration 007 — confirm rename + column add + backfill (`tests/integration/test_lake_multi_asset.py`).
- Integration test for the new lake upsert methods (each new table).
- Integration test for `IngestPipeline` upserting a non-equity bundle.
- Integration test for `Ranker` asset-class filtering.
- Live tests under `tests/integration/live/` only if free-tier EODHD reaches non-equity endpoints; otherwise skip-gated.

## 8. Acceptance criteria

- All existing 403 tests still pass.
- New tests cover: classifier, each parser, migration, lake upserts, pipeline bundle, ranker filter, CLI flag.
- `uv run stonks db init` migrates existing equity data without loss; `instruments` table exists with `asset_class='equity'` for prior rows.
- `uv run stonks ingest prices --tickers BTC-USD.CC` works against free-tier EODHD without errors.
- `uv run ruff check .` clean.

## 9. Migration & rollback

The rename (`tickers` → `instruments`) is a one-way change in this codebase. Rollback path: if migration 007 fails, the transaction wrapper in `DuckDBLake.migrate()` rolls back the SQL but `schema_migrations` won't record version 7, so re-running is safe. No data is lost — only the table name changes; row contents are preserved.
