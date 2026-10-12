-- The resumable EODHD backfill (`stonks ingest backfill`, docs/backfill.md).
--
-- `backfill_units`: one row per unit of work of a phase (an exchange, a
-- ticker, a calendar, a currency pair). `status` is pending (not started or
-- in progress), retry (failed, tried again after `next_attempt_at`), done or
-- failed (gave up after the last attempt). `cursor` keeps how far a unit that
-- runs in steps got (an intraday window, a calendar month), so a stop
-- resumes there. `calls` is what the unit spent in vendor API calls.
--
-- `backfill_phases`: when each phase last listed its units.
--
-- `backfill_calls`: API calls spent per UTC day, plus the vendor's own count
-- (`vendor_used`) read at `vendor_read_at`, which also counts other
-- processes on the same key.

CREATE TABLE backfill_units (
    phase TEXT NOT NULL,
    unit_key TEXT NOT NULL,
    exchange TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'retry', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    rows INTEGER NOT NULL DEFAULT 0,
    cursor TEXT,
    last_error TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (phase, unit_key)
);

CREATE INDEX ix_backfill_units_next ON backfill_units (phase, status, priority, unit_key);

CREATE TABLE backfill_phases (
    phase TEXT PRIMARY KEY,
    listed_at TEXT NOT NULL,
    units INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE backfill_calls (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    vendor_used INTEGER,
    vendor_limit INTEGER,
    vendor_read_at TEXT
);
