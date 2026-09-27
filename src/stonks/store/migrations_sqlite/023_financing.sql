-- Financing of short and margin books (roadmap 16.1).
--
-- A short book's paper broker is built fresh each tick, so it cannot
-- remember when it last charged financing. The tick reads the date here,
-- hands it to broker.accrue(since=...), and writes the new date and the
-- charges in the transaction that writes its snapshot.

CREATE TABLE IF NOT EXISTS financing_accruals (
    portfolio_id    TEXT PRIMARY KEY,
    accrued_through TEXT NOT NULL,                 -- ISO date
    updated_at      TEXT NOT NULL                  -- ISO-8601 UTC
);

-- One row per charge: the borrow fee of one short, or debit interest on
-- negative cash. amount is the cash change (negative: a charge).
CREATE TABLE IF NOT EXISTS financing_charges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL,
    tick_id      TEXT,
    as_of        TEXT NOT NULL,                    -- ISO date
    ticker       TEXT,                             -- NULL for debit interest
    kind         TEXT NOT NULL CHECK (kind IN ('borrow_fee', 'debit_interest')),
    amount       REAL NOT NULL,
    days         INTEGER NOT NULL,
    recorded_at  TEXT NOT NULL                     -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_financing_charges_portfolio
    ON financing_charges (portfolio_id, as_of);
