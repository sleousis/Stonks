-- Foreign exchange rates: one daily observation per currency pair.
--
-- fx_rates (base_currency, quote_currency, observation_date, rate, source;
--           PK (base_currency, quote_currency, observation_date))
--
-- Vendor-agnostic shape (same precedent as defi_tvl): EODHD forex is the
-- first source, but the lake never sees its field names or its ticker
-- format (EURUSD.FOREX). Currencies are ISO 4217 upper-case codes.
--
-- `rate` is the units of quote_currency one unit of base_currency buys on
-- observation_date (the day's close). EUR/USD 1.10 means 1 EUR = 1.10 USD.
-- Readers take the latest rate on or before the day they convert, use the
-- inverse pair when only that one is stored, and cross through USD.
-- `source` is provenance (the DataSource.source_id that wrote the row) and
-- deliberately not part of the key: one canonical series per pair.
--
-- Upserts are idempotent and last-write-wins, so a re-run is a no-op and
-- vendor revisions land in place.

CREATE TABLE fx_rates (
    base_currency     VARCHAR NOT NULL,    -- ISO 4217, e.g. 'EUR'
    quote_currency    VARCHAR NOT NULL,    -- ISO 4217, e.g. 'USD'
    observation_date  DATE    NOT NULL,    -- day of the close
    rate              DOUBLE  NOT NULL,    -- quote units per 1 base unit
    source            VARCHAR NOT NULL,    -- DataSource.source_id, e.g. 'eodhd'
    PRIMARY KEY (base_currency, quote_currency, observation_date)
);
