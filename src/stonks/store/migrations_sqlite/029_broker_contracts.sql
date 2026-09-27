-- The broker contract cache (roadmap 19.2, docs/design/live-trading.md
-- section 2, "Contract resolution").
--
-- One row per (broker, ticker): the broker's id for the contract our
-- ticker trades as, with the facts orders need (minimum tick, currency,
-- price units). Unique on (broker, con_id) too, so positions map back to
-- one ticker. Rows older than [brokers.ibkr] contract_max_age_days are
-- looked up again. Nothing here is a secret.

CREATE TABLE IF NOT EXISTS broker_contracts (
    broker           TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    con_id           TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    sec_type         TEXT NOT NULL,
    exchange         TEXT NOT NULL,
    primary_exchange TEXT,
    currency         TEXT NOT NULL CHECK (length(currency) = 3),
    min_tick         REAL NOT NULL CHECK (min_tick > 0),
    price_magnifier  INTEGER NOT NULL DEFAULT 1 CHECK (price_magnifier >= 1),
    trading_class    TEXT,
    isin             TEXT,
    verified_at      TEXT NOT NULL,
    PRIMARY KEY (broker, ticker)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_broker_contracts_con_id
    ON broker_contracts(broker, con_id);
