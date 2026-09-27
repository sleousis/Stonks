-- Options (roadmap 17.1). Vendor-agnostic: every options DataSource fills
-- these tables with the same columns.
--
-- option_contracts: one row per listed series. contract_id is the canonical
-- id '<underlying>:<expiry>:<C|P>:<strike>[:<multiplier>]' (core/options.py);
-- the OCC symbol is kept as one vendor key among many. right, style and
-- settlement are normalised literals. multiplier is never assumed: adjusted
-- contracts after a split or merger deliver something other than 100.
CREATE TABLE option_contracts (
    contract_id  VARCHAR PRIMARY KEY,
    underlying   VARCHAR NOT NULL,
    expiry       DATE    NOT NULL,
    strike       DOUBLE  NOT NULL CHECK (strike > 0),
    "right"      VARCHAR NOT NULL CHECK ("right" IN ('call', 'put')),
    style        VARCHAR NOT NULL DEFAULT 'american' CHECK (style IN ('american', 'european')),
    multiplier   DOUBLE  NOT NULL DEFAULT 100 CHECK (multiplier > 0),
    settlement   VARCHAR NOT NULL DEFAULT 'physical' CHECK (settlement IN ('physical', 'cash')),
    currency     VARCHAR,
    exchange     VARCHAR,
    occ_symbol   VARCHAR,
    first_seen   DATE,
    last_seen    DATE
);
CREATE INDEX option_contracts_underlying ON option_contracts (underlying, expiry);

-- option_quotes: one end-of-day snapshot per contract, day and source. The
-- Greeks and implied volatility are the vendor's (vendor_*), kept as
-- reported; our own analytics are recomputed from the prices and never
-- stored as the source of truth. underlying_price is the underlying's
-- close the vendor priced against, when it says.
CREATE TABLE option_quotes (
    contract_id       VARCHAR NOT NULL,
    as_of             DATE    NOT NULL,
    source            VARCHAR NOT NULL,
    underlying        VARCHAR NOT NULL,
    bid               DOUBLE,
    ask               DOUBLE,
    "last"            DOUBLE,
    volume            DOUBLE,
    open_interest     DOUBLE,
    underlying_price  DOUBLE,
    vendor_iv         DOUBLE,
    vendor_delta      DOUBLE,
    vendor_gamma      DOUBLE,
    vendor_theta      DOUBLE,
    vendor_vega       DOUBLE,
    vendor_rho        DOUBLE,
    PRIMARY KEY (contract_id, as_of, source)
);
CREATE INDEX option_quotes_chain ON option_quotes (underlying, as_of);
