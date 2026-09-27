-- Roadmap 19.3: daily stock borrow rates for short sales.
--
-- borrow_rates (ticker, as_of, source, currency, isin, available_shares,
--               fee_rate_annual, rebate_rate_annual;
--               PK (ticker, as_of, source))
--
-- Vendor neutral: IBKR's short stock file is the first source, but the lake
-- never sees its column names (FEERATE, AVAILABLE) or its symbols ("BRK B").
-- Adapters map them to our tickers and to fractions at parse time.
--
-- fee_rate_annual is the yearly borrow fee as a fraction of the short's
-- market value (0.0025 = 0.25 %), the unit execution.borrow.BorrowQuote
-- uses. rebate_rate_annual is the yearly rebate paid on the short sale's
-- cash, as a fraction, and may be negative for hard to borrow names.
-- available_shares is what the lender says it can supply (a floor when the
-- vendor writes ">10000000"); 0 means no locate, NULL means not stated.
--
-- source is part of the key: two brokers quote different rates for the
-- same stock on the same day. Readers take the latest row on or before the
-- day they need. Upserts are idempotent and last-write-wins.

CREATE TABLE borrow_rates (
    ticker              VARCHAR NOT NULL,   -- our ticker, e.g. 'AAPL.US'
    as_of               DATE    NOT NULL,   -- the day the rate applies to
    source              VARCHAR NOT NULL,   -- DataSource.source_id, e.g. 'ibkr_borrow'
    currency            VARCHAR,            -- ISO 4217 of the listing
    isin                VARCHAR,
    available_shares    DOUBLE,             -- NULL: not stated
    fee_rate_annual     DOUBLE  NOT NULL,   -- fraction per year
    rebate_rate_annual  DOUBLE,             -- fraction per year, may be negative
    PRIMARY KEY (ticker, as_of, source)
);
