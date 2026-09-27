-- Corporate actions handled per portfolio (TO-05).
--
-- The tick used to apply the events with ex_date in (last snapshot, as_of].
-- An event whose row reached the lake after its ex-date tick was lost for
-- good, and one applied before its ex-date bar doubled the book at the old
-- quote. Now each (portfolio, ticker, ex_date, kind) is handled exactly
-- once, whenever it arrives, and only once the ex-date bar is in the lake.
--
-- A row is written in the transaction that writes the tick's snapshot,
-- also when the event touched nothing (the position was bought after the
-- ex-date), so it is never looked at again.

CREATE TABLE IF NOT EXISTS corporate_action_ledger (
    portfolio_id    TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    ex_date         TEXT NOT NULL,                 -- ISO date
    kind            TEXT NOT NULL CHECK (kind IN ('split', 'dividend')),
    value           REAL NOT NULL,                 -- split ratio or cash per share
    quantity_before REAL NOT NULL,
    quantity_after  REAL NOT NULL,
    cash_delta      REAL NOT NULL,
    tick_id         TEXT,
    applied_at      TEXT NOT NULL,                 -- ISO-8601 UTC
    PRIMARY KEY (portfolio_id, ticker, ex_date, kind)
);

-- Where each existing portfolio's ledger starts. Events on or before its
-- latest tick snapshot at upgrade time were handled by the old rule, so
-- they count as done. Portfolios created later have no row: every event
-- in their snapshot history is looked at.
CREATE TABLE IF NOT EXISTS corporate_action_ledger_start (
    portfolio_id TEXT PRIMARY KEY,
    start_as_of  TEXT NOT NULL                     -- ISO date
);

INSERT OR IGNORE INTO corporate_action_ledger_start (portfolio_id, start_as_of)
SELECT portfolio_id, MAX(as_of)
  FROM portfolio_snapshots
 WHERE as_of IS NOT NULL AND portfolio_id IS NOT NULL AND source = 'tick'
 GROUP BY portfolio_id;
