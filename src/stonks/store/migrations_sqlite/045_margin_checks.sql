-- Margin accounts (roadmap 19.13, docs/design/live-trading.md section 23).
--
-- margin_checks   one row each time Stonks reads a margin account's
--                 balances: the tick of its live book (source 'tick') and
--                 the live_margin job during the session ('monitor').
--   cushion       excess liquidity over equity. At or below 0 the broker
--                 may sell positions on its own.
--   level         ok, warn (an alert), reduce (the next run closes
--                 positions until the cushion is back) or call (below 0).
--   reported_type the account type the broker reported ('margin', 'cash'
--                 or NULL when it did not say).
-- Rows are written once and never changed. The console reads the newest.

CREATE TABLE IF NOT EXISTS margin_checks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id       TEXT NOT NULL,
    checked_at         TEXT NOT NULL,
    source             TEXT NOT NULL CHECK (source IN ('tick', 'monitor')),
    currency           TEXT NOT NULL,
    equity             REAL NOT NULL,
    initial_margin     REAL NOT NULL,
    maintenance_margin REAL NOT NULL,
    excess_liquidity   REAL,
    available_funds    REAL,
    buying_power       REAL,
    cushion            REAL,
    level              TEXT NOT NULL CHECK (level IN ('ok', 'warn', 'reduce', 'call')),
    reported_type      TEXT CHECK (reported_type IS NULL OR reported_type IN ('cash', 'margin'))
);

CREATE INDEX IF NOT EXISTS idx_margin_checks_portfolio
    ON margin_checks (portfolio_id, checked_at);
