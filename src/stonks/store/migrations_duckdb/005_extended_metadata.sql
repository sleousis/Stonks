-- Extended metadata coverage: institutional / fund holders, full earnings
-- event + analyst-trend surfaces, ESG breakdown, external identifiers,
-- GIC classification, address, listings, officers, ticker snapshot row.
-- Drops the now-superseded analyst_estimates table; rebuilds
-- insider_transactions and analyst_ratings to capture richer fields and a
-- proper time-series shape.
--
-- Vendor-agnostic: every column describes a domain concept, never a vendor
-- JSON shape. Fields whose vocabulary varies across vendors
-- (security_type, before_after_market, period_relative,
-- acquired_disposed, holder_kind) are stored as VARCHAR; Pydantic enforces
-- the canonical literal values at the application boundary.

-- -----------------------------------------------------------------------
-- 1. tickers: drop volatile columns (now in ticker_snapshots /
--    esg_snapshots / employee_count) and add the ~22 static fields that
--    were previously dropped on the floor.
-- -----------------------------------------------------------------------

ALTER TABLE tickers DROP COLUMN beta;
ALTER TABLE tickers DROP COLUMN short_percent;
ALTER TABLE tickers DROP COLUMN insider_ownership_percent;
ALTER TABLE tickers DROP COLUMN institutional_ownership_percent;
ALTER TABLE tickers DROP COLUMN employee_count;
ALTER TABLE tickers DROP COLUMN esg_score;

-- Lifecycle
ALTER TABLE tickers ADD COLUMN delisted_date DATE;
ALTER TABLE tickers ADD COLUMN security_type VARCHAR;

-- External identifiers
ALTER TABLE tickers ADD COLUMN cusip VARCHAR;
ALTER TABLE tickers ADD COLUMN cik VARCHAR;
ALTER TABLE tickers ADD COLUMN isin VARCHAR;
ALTER TABLE tickers ADD COLUMN open_figi VARCHAR;
ALTER TABLE tickers ADD COLUMN lei VARCHAR;
ALTER TABLE tickers ADD COLUMN employer_id_number VARCHAR;
ALTER TABLE tickers ADD COLUMN primary_ticker VARCHAR;

-- GIC classification
ALTER TABLE tickers ADD COLUMN gic_sector VARCHAR;
ALTER TABLE tickers ADD COLUMN gic_group VARCHAR;
ALTER TABLE tickers ADD COLUMN gic_industry VARCHAR;
ALTER TABLE tickers ADD COLUMN gic_sub_industry VARCHAR;

-- Address + contact
ALTER TABLE tickers ADD COLUMN address_street VARCHAR;
ALTER TABLE tickers ADD COLUMN address_city VARCHAR;
ALTER TABLE tickers ADD COLUMN address_state VARCHAR;
ALTER TABLE tickers ADD COLUMN address_country VARCHAR;
ALTER TABLE tickers ADD COLUMN address_zip VARCHAR;
ALTER TABLE tickers ADD COLUMN phone VARCHAR;

-- Misc
ALTER TABLE tickers ADD COLUMN description VARCHAR;
ALTER TABLE tickers ADD COLUMN updated_at TIMESTAMP;

-- -----------------------------------------------------------------------
-- 2. insider_transactions: drop and recreate.
--    Real trade date is now `transaction_date`; SEC filing date is
--    `filing_date`. Adds owner_cik / owner_title / acquired_disposed /
--    post_transaction_amount / sec_link. Drops vendor_id (never populated
--    by EODHD; better natural keys exist).
-- -----------------------------------------------------------------------

DROP INDEX IF EXISTS uq_insider_natural;
DROP TABLE insider_transactions;
DROP SEQUENCE IF EXISTS insider_transactions_id_seq;
CREATE SEQUENCE insider_transactions_id_seq;
CREATE TABLE insider_transactions (
    id                      INTEGER PRIMARY KEY DEFAULT nextval('insider_transactions_id_seq'),
    ticker                  VARCHAR NOT NULL,
    transaction_date        DATE    NOT NULL,
    filing_date             DATE,
    owner_name              VARCHAR,
    owner_cik               VARCHAR,
    owner_relation          VARCHAR,
    owner_title             VARCHAR,
    transaction_code        VARCHAR,
    acquired_disposed       VARCHAR,    -- 'A' or 'D' (SEC Form 4 standard)
    shares                  DOUBLE,
    price                   DOUBLE,
    value                   DOUBLE,
    post_transaction_amount DOUBLE,
    sec_link                VARCHAR
);
-- Natural dedup key: same owner + code + shares + sec_link on the same trade
-- date is the same trade. Including sec_link helps dedup the political
-- STOCK Act disclosures where shares is NULL.
CREATE UNIQUE INDEX uq_insider_natural ON insider_transactions (
    ticker, transaction_date, owner_name, transaction_code, shares, sec_link
);

-- -----------------------------------------------------------------------
-- 3. analyst_estimates: drop. Superseded by earnings_announcements
--    (the actual event) + analyst_forecasts (dispersion/revisions).
-- -----------------------------------------------------------------------

DROP TABLE analyst_estimates;

-- -----------------------------------------------------------------------
-- 4. analyst_ratings: rebuild as a proper time series with snapshot_date.
--    Was a single-row-per-ticker table; now keyed by (ticker, snapshot_date)
--    and ingested via change-detection (only inserts new rows when the
--    consensus actually moves).
-- -----------------------------------------------------------------------

DROP TABLE analyst_ratings;
CREATE TABLE analyst_ratings (
    ticker        VARCHAR NOT NULL,
    snapshot_date DATE    NOT NULL,
    rating        DOUBLE,
    target_price  DOUBLE,
    strong_buy    INTEGER DEFAULT 0,
    buy           INTEGER DEFAULT 0,
    hold          INTEGER DEFAULT 0,
    sell          INTEGER DEFAULT 0,
    strong_sell   INTEGER DEFAULT 0,
    PRIMARY KEY (ticker, snapshot_date)
);

-- -----------------------------------------------------------------------
-- 5. New tables.
-- -----------------------------------------------------------------------

-- Volatile per-ticker metrics (beta, short interest, ownership %).
-- Time-series; ingested via change-detection.
CREATE TABLE ticker_snapshots (
    ticker               VARCHAR NOT NULL,
    snapshot_date        DATE    NOT NULL,
    beta                 DOUBLE,
    short_percent        DOUBLE,
    percent_insiders     DOUBLE,
    percent_institutions DOUBLE,
    PRIMARY KEY (ticker, snapshot_date)
);

-- Top-N institutional + fund holders. holder_kind discriminates between
-- the two buckets (vendors typically expose ~20 of each).
CREATE TABLE institutional_holders (
    ticker            VARCHAR NOT NULL,
    holder_kind       VARCHAR NOT NULL,    -- 'institution' | 'fund'
    name              VARCHAR NOT NULL,
    snapshot_date     DATE    NOT NULL,
    total_shares_pct  DOUBLE,
    total_assets_pct  DOUBLE,
    current_shares    BIGINT,
    change_shares     BIGINT,
    change_pct        DOUBLE,
    PRIMARY KEY (ticker, holder_kind, name, snapshot_date)
);

-- Earnings events: actual reported EPS + analyst consensus + surprise.
-- Future quarters carry eps_estimate but not eps_actual until announced.
CREATE TABLE earnings_announcements (
    ticker              VARCHAR NOT NULL,
    period_end          DATE    NOT NULL,
    report_date         DATE,
    before_after_market VARCHAR,    -- 'before' | 'during' | 'after' (normalized)
    currency            VARCHAR,
    eps_actual          DOUBLE,
    eps_estimate        DOUBLE,
    eps_difference      DOUBLE,
    surprise_percent    DOUBLE,
    PRIMARY KEY (ticker, period_end)
);

-- Analyst dispersion + revenue forecasts + EPS revision history.
-- period_relative discriminates because the same period_end can carry
-- separate quarterly/yearly aggregates.
CREATE TABLE analyst_forecasts (
    ticker                          VARCHAR NOT NULL,
    period_end                      DATE    NOT NULL,
    period_relative                 VARCHAR NOT NULL,    -- normalized literal
    growth                          DOUBLE,
    eps_estimate_avg                DOUBLE,
    eps_estimate_low                DOUBLE,
    eps_estimate_high               DOUBLE,
    eps_estimate_year_ago           DOUBLE,
    eps_estimate_n_analysts         INTEGER,
    eps_estimate_growth             DOUBLE,
    revenue_estimate_avg            DOUBLE,
    revenue_estimate_low            DOUBLE,
    revenue_estimate_high           DOUBLE,
    revenue_estimate_year_ago       DOUBLE,
    revenue_estimate_n_analysts     INTEGER,
    revenue_estimate_growth         DOUBLE,
    eps_trend_current               DOUBLE,
    eps_trend_7d_ago                DOUBLE,
    eps_trend_30d_ago               DOUBLE,
    eps_trend_60d_ago               DOUBLE,
    eps_trend_90d_ago               DOUBLE,
    eps_revisions_up_7d             INTEGER,
    eps_revisions_up_30d            INTEGER,
    eps_revisions_down_7d           INTEGER,
    eps_revisions_down_30d          INTEGER,
    PRIMARY KEY (ticker, period_end, period_relative)
);

-- ESG ratings header. Time-series via rating_date + change-detection.
CREATE TABLE esg_snapshots (
    ticker                 VARCHAR NOT NULL,
    rating_date            DATE    NOT NULL,
    total_esg              DOUBLE,
    total_esg_percentile   DOUBLE,
    environment_score      DOUBLE,
    environment_percentile DOUBLE,
    social_score           DOUBLE,
    social_percentile      DOUBLE,
    governance_score       DOUBLE,
    governance_percentile  DOUBLE,
    controversy_level      INTEGER,
    PRIMARY KEY (ticker, rating_date)
);

-- ESG controversial-activity flags (child of esg_snapshots).
CREATE TABLE esg_activities (
    ticker      VARCHAR NOT NULL,
    rating_date DATE    NOT NULL,
    activity    VARCHAR NOT NULL,
    involvement VARCHAR NOT NULL,    -- typically 'Yes' / 'No'
    PRIMARY KEY (ticker, rating_date, activity)
);

-- Secondary listings of the same security on other exchanges.
CREATE TABLE cross_listings (
    ticker        VARCHAR NOT NULL,
    exchange      VARCHAR NOT NULL,
    exchange_code VARCHAR NOT NULL,
    name          VARCHAR,
    PRIMARY KEY (ticker, exchange, exchange_code)
);

-- Current officer roster. Replace-on-fetch (per-ticker delete-then-insert),
-- not a time series, since vendors return a current-state list with no
-- per-officer date.
CREATE TABLE officers (
    ticker     VARCHAR NOT NULL,
    name       VARCHAR NOT NULL,
    title      VARCHAR,
    year_born  INTEGER,
    PRIMARY KEY (ticker, name)
);
