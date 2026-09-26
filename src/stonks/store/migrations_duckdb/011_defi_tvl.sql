-- DeFi total value locked (TVL): one daily observation per blockchain.
--
-- defi_tvl (chain, observation_date, tvl_usd, source; PK (chain, observation_date))
--
-- Vendor-agnostic shape (same precedent as macro_indicators): DefiLlama is
-- the first source, but the lake never sees its field names. `chain` is the
-- canonical lower-case chain name ('ethereum', 'solana', 'arbitrum nova');
-- adapters normalize their vendor spelling at parse time. The set of chains
-- is open, so the column is free-text rather than a closed literal.
--
-- `observation_date` is the UTC calendar day the vendor stamps the value
-- with, NOT the day it became public: a strategy must lag it itself (the
-- TVL-deviation strategy only uses the value stamped d from bar d+1 on).
-- `tvl_usd` is the aggregate TVL in US dollars (NULL = not published).
-- `source` is provenance (the DataSource.source_id that wrote the row) and
-- deliberately not part of the key: one canonical series per chain.
--
-- Upserts are idempotent and last-write-wins on (chain, observation_date),
-- so a re-run is a no-op and vendor revisions land in place.

CREATE TABLE defi_tvl (
    chain             VARCHAR NOT NULL,    -- canonical lower-case, e.g. 'ethereum'
    observation_date  DATE    NOT NULL,    -- UTC day the value is stamped with
    tvl_usd           DOUBLE,              -- total value locked, USD
    source            VARCHAR NOT NULL,    -- DataSource.source_id, e.g. 'defillama'
    PRIMARY KEY (chain, observation_date)
);
