-- Reconciliation and drift (roadmap 19.5, docs/design/live-trading.md section 6).
--
-- One row per check of a live portfolio against its broker: start of day
-- (sod), before a submit (submit), end of day (eod) or by hand (adhoc).
--   status        clean: nothing unexplained. warn: only items that alert
--                 (a stuck order, a missing commission). drift: a material
--                 item, which opens the portfolio's broker_drift halt and
--                 pauses its auto subscriptions. outage: the broker did not
--                 answer (the day is skipped, a long outage pauses). fault:
--                 the broker answered wrongly (wrong account, login
--                 refused), which pauses at once.
--   items_json    the unexplained drift items (execution.drift.DriftItem).
--   explained_json items the check explained or fixed itself (a stale
--                 order it cancelled at the start of the day).
--   external_json the owner's own positions and hand-placed orders in the
--                 shared account: never traded, never drift.
--   summary_json  what reconciliation did (fills booked, orders updated).
--   halt_id       the broker_drift halt this check opened or found open.
--   paused_json   the auto subscriptions this check paused.
-- Rows are written once and never changed after the check finishes.

CREATE TABLE IF NOT EXISTS reconcile_reports (
    id             TEXT PRIMARY KEY,                  -- rec_<hex>
    portfolio_id   TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('sod', 'submit', 'eod', 'adhoc')),
    as_of          TEXT NOT NULL,                     -- the session day (YYYY-MM-DD)
    taken_at       TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('clean', 'warn', 'drift', 'outage', 'fault')),
    items_json     TEXT NOT NULL DEFAULT '[]',
    explained_json TEXT NOT NULL DEFAULT '[]',
    external_json  TEXT NOT NULL DEFAULT '{}',
    summary_json   TEXT NOT NULL DEFAULT '{}',
    detail         TEXT,
    halt_id        INTEGER REFERENCES risk_halts(id),
    paused_json    TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_reconcile_reports_portfolio
    ON reconcile_reports(portfolio_id, taken_at DESC);
