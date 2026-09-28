-- Roadmap 23.14: what each fund (an ETF) holds, point in time.
--
-- fund_holdings (fund, holding, as_of, source, weight, name, sector,
--                country, known_at; PK (fund, holding, as_of, source))
--
-- fund and holding are our tickers ('SPY.US', 'AAPL.US') when the vendor
-- names a listing, else the vendor's own code. weight is the holding's
-- share of the fund's assets as a fraction (0.07 = 7 %). country is ISO
-- 3166-1 alpha-2, like instruments.country_iso.
--
-- as_of is the day the vendor says the list describes. known_at is when
-- Stonks first stored the row (naive UTC). A re-run updates the weight but
-- keeps known_at. A read on a day takes, per fund, the latest as_of whose
-- rows were known by the end of that day (principle P12), from one source.

CREATE TABLE fund_holdings (
    fund      VARCHAR   NOT NULL,
    holding   VARCHAR   NOT NULL,
    as_of     DATE      NOT NULL,
    source    VARCHAR   NOT NULL,   -- DataSource.source_id, e.g. 'eodhd'
    weight    DOUBLE    NOT NULL,   -- fraction of the fund's assets
    name      VARCHAR,
    sector    VARCHAR,
    country   VARCHAR,              -- ISO 3166-1 alpha-2
    known_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (fund, holding, as_of, source)
);
