-- Reference data the backfill fills (docs/backfill.md): exchange trading
-- hours and holidays, US Treasury rates, and the IPO and split calendars.
-- Index members go to index_constituent_snapshots and _changes (016) and
-- identifiers to the instruments columns (005), so they need no table.
--
-- Values a vendor can revise follow principle P12 like migration 025: the
-- table is the latest view, and its <table>_versions twin keeps every
-- version with known_at, the time Stonks first saw it (naive UTC). A
-- decision reads the latest version known by then. A later migration that
-- adds a column to one of these tables must add it to its twin too.

-- exchange_details: one row per exchange and source. Times are local
-- (open_local, close_local, in timezone) and UTC. operating_mics is a
-- comma list of ISO 10383 MICs, working_days a comma list of ISO weekdays
-- (1 = Monday).
CREATE TABLE exchange_details (
    exchange        VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    name            VARCHAR,
    operating_mics  VARCHAR,
    country         VARCHAR,
    currency        VARCHAR,
    timezone        VARCHAR,
    open_local      TIME,
    close_local     TIME,
    open_utc        TIME,
    close_utc       TIME,
    working_days    VARCHAR,
    PRIMARY KEY (exchange, source)
);

CREATE TABLE exchange_details_versions (
    exchange        VARCHAR   NOT NULL,
    source          VARCHAR   NOT NULL,
    name            VARCHAR,
    operating_mics  VARCHAR,
    country         VARCHAR,
    currency        VARCHAR,
    timezone        VARCHAR,
    open_local      TIME,
    close_local     TIME,
    open_utc        TIME,
    close_utc       TIME,
    working_days    VARCHAR,
    known_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (exchange, source, known_at)
);

-- exchange_holidays: days an exchange is shut, or closes early
-- (early_close set, local time). kind is official, bank or other.
-- known_at is when Stonks first saw the day.
CREATE TABLE exchange_holidays (
    exchange      VARCHAR   NOT NULL,
    holiday_date  DATE      NOT NULL,
    source        VARCHAR   NOT NULL,
    name          VARCHAR,
    kind          VARCHAR   NOT NULL CHECK (kind IN ('official', 'bank', 'other')),
    early_close   TIME,
    known_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (exchange, holiday_date, source)
);

-- treasury_rates: US Treasury rates in percent. curve: bill (by weeks to
-- maturity), par_nominal and par_real (the par yield curves), long_term
-- and long_term_real (the long-term composites). measure: yield, or for
-- bills bank_discount, coupon_equivalent and their _avg forms. tenor is a
-- normalized code (4W, 1M, 1.5M, 10Y, 20Y, 10Y+). maturity_date and cusip
-- name the bill a bill rate is for.
CREATE TABLE treasury_rates (
    curve             VARCHAR NOT NULL CHECK (curve IN
                          ('bill', 'par_nominal', 'par_real', 'long_term', 'long_term_real')),
    tenor             VARCHAR NOT NULL,
    measure           VARCHAR NOT NULL CHECK (measure IN ('yield', 'bank_discount',
                          'coupon_equivalent', 'bank_discount_avg', 'coupon_equivalent_avg')),
    observation_date  DATE    NOT NULL,
    source            VARCHAR NOT NULL,
    rate              DOUBLE,
    maturity_date     DATE,
    cusip             VARCHAR,
    PRIMARY KEY (curve, tenor, measure, observation_date, source)
);

CREATE TABLE treasury_rates_versions (
    curve             VARCHAR   NOT NULL,
    tenor             VARCHAR   NOT NULL,
    measure           VARCHAR   NOT NULL,
    observation_date  DATE      NOT NULL,
    source            VARCHAR   NOT NULL,
    rate              DOUBLE,
    maturity_date     DATE,
    cusip             VARCHAR,
    known_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (curve, tenor, measure, observation_date, source, known_at)
);

-- ipo_calendar: one row per offering, keyed by its ticker and filing date
-- (the listing date when no filing date is known). Prices are per share in
-- currency, NULL when not set yet. status: expected, filed, priced,
-- amended, withdrawn or other. exchange is the ticker's exchange code.
CREATE TABLE ipo_calendar (
    ticker        VARCHAR NOT NULL,
    filing_date   DATE    NOT NULL,
    source        VARCHAR NOT NULL,
    exchange      VARCHAR,
    name          VARCHAR,
    currency      VARCHAR,
    amended_date  DATE,
    listing_date  DATE,
    price_low     DOUBLE,
    price_high    DOUBLE,
    offer_price   DOUBLE,
    shares        DOUBLE,
    status        VARCHAR NOT NULL CHECK (status IN
                      ('expected', 'filed', 'priced', 'amended', 'withdrawn', 'other')),
    PRIMARY KEY (ticker, filing_date, source)
);

CREATE TABLE ipo_calendar_versions (
    ticker        VARCHAR   NOT NULL,
    filing_date   DATE      NOT NULL,
    source        VARCHAR   NOT NULL,
    exchange      VARCHAR,
    name          VARCHAR,
    currency      VARCHAR,
    amended_date  DATE,
    listing_date  DATE,
    price_low     DOUBLE,
    price_high    DOUBLE,
    offer_price   DOUBLE,
    shares        DOUBLE,
    status        VARCHAR   NOT NULL,
    known_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, filing_date, source, known_at)
);

-- split_calendar: announced and past splits for the whole market, as
-- announced (stock_splits holds the per-ticker history). old_shares become
-- new_shares on split_date.
CREATE TABLE split_calendar (
    ticker      VARCHAR NOT NULL,
    split_date  DATE    NOT NULL,
    source      VARCHAR NOT NULL,
    old_shares  DOUBLE,
    new_shares  DOUBLE,
    optionable  BOOLEAN,
    PRIMARY KEY (ticker, split_date, source)
);

CREATE TABLE split_calendar_versions (
    ticker      VARCHAR   NOT NULL,
    split_date  DATE      NOT NULL,
    source      VARCHAR   NOT NULL,
    old_shares  DOUBLE,
    new_shares  DOUBLE,
    optionable  BOOLEAN,
    known_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, split_date, source, known_at)
);
