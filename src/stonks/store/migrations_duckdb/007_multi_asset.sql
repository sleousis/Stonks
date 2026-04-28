-- Multi-asset support: rename `tickers` to `instruments`, add an
-- asset_class column, and create per-class profile tables for crypto,
-- bonds, and commodities.
--
-- The `tickers` table was always the abstract instrument table — CUSIP /
-- ISIN / OpenFigi / LEI live there, the comment block already calls the
-- row "the abstract instrument" — but the name was equity-loaded. With
-- crypto/bond/commodity rows joining the table, the rename keeps the
-- domain vocabulary honest.
--
-- Equity-specific tables (fundamentals, dividends, insider_transactions,
-- analyst_*, esg_*, news, splits, segmentation, employees,
-- shares_outstanding, market_cap_history, officers, ticker_snapshots,
-- earnings_announcements, cross_listings, institutional_holders) keep
-- the same shape and PK; they simply have zero rows for non-equity
-- instruments. No FK enforcement against instruments(id) — preserves
-- the existing pragma of soft, soft-failing ingest where a profile may
-- arrive after its first price bar.
--
-- Vendor-agnostic: every column describes a domain concept. Fields whose
-- vocabulary varies across vendors (asset_class, consensus_type,
-- issuer_kind, bond_kind, contract_kind) are stored as VARCHAR; Pydantic
-- enforces the canonical literal values at the application boundary.

-- -----------------------------------------------------------------------
-- 1. Rename + extend the instrument table.
-- -----------------------------------------------------------------------

ALTER TABLE tickers RENAME TO instruments;
ALTER TABLE instruments ADD COLUMN asset_class VARCHAR;

-- Existing rows are all equities (this codebase has been equity-only up
-- to this migration). Backfill so every row carries a class.
UPDATE instruments SET asset_class = 'equity' WHERE asset_class IS NULL;

-- -----------------------------------------------------------------------
-- 2. Per-class profile tables.
-- -----------------------------------------------------------------------

-- Static-ish profile for a crypto asset. Refreshed periodically;
-- supply *trajectory* is captured by re-snapshotting in place (one row
-- per ticker), not as a separate time series.
CREATE TABLE crypto_profiles (
    ticker               VARCHAR PRIMARY KEY,
    base_symbol          VARCHAR,
    quote_symbol         VARCHAR,
    blockchain           VARCHAR,
    consensus_type       VARCHAR,
    circulating_supply   DOUBLE,
    total_supply         DOUBLE,
    max_supply           DOUBLE,
    supply_snapshot_date DATE
);

-- Static profile for a bond. Yield + price move daily and live in
-- bond_yield_history; this row is the static contract (terms + issuer).
CREATE TABLE bond_profiles (
    ticker            VARCHAR PRIMARY KEY,
    issuer_name       VARCHAR,
    issuer_kind       VARCHAR,
    bond_kind         VARCHAR,
    coupon_rate       DOUBLE,
    coupon_frequency  INTEGER,
    face_value        DOUBLE,
    currency          VARCHAR,
    issue_date        DATE,
    maturity_date     DATE,
    credit_rating     VARCHAR
);

-- Daily yield + clean-price observations for a bond. clean_price is
-- quoted as percent-of-par; yield_to_maturity is a percent.
CREATE TABLE bond_yield_history (
    ticker             VARCHAR NOT NULL,
    date               DATE    NOT NULL,
    yield_to_maturity  DOUBLE,
    clean_price        DOUBLE,
    PRIMARY KEY (ticker, date)
);

-- Contract metadata for a commodity instrument (futures, spot,
-- continuous, or index). The bar series itself lives in `bars` keyed by
-- ticker; this row carries what kind of commodity exposure the ticker
-- represents and the standardized contract details.
CREATE TABLE commodity_contracts (
    ticker             VARCHAR PRIMARY KEY,
    underlying_symbol  VARCHAR,
    contract_kind      VARCHAR,
    contract_month     VARCHAR,
    expiry_date        DATE,
    contract_size      DOUBLE,
    contract_unit      VARCHAR
);
