-- Intraday risk (roadmap 21.3.2): risk_halts gains the 'intraday_loss' kind,
-- opened when a book loses more than its limit within a few minutes.
--
-- SQLite cannot change a CHECK, so risk_halts is rebuilt with its rows,
-- ids, indexes and triggers, as migration 028 did. The order matters:
--   * reconcile_reports.halt_id references risk_halts(id), and dropping a
--     referenced table with rows pointing at it fails, so reconcile_reports
--     is kept aside, dropped first and rebuilt after with its rows.
--   * AUTOINCREMENT counters live in sqlite_sequence and a DROP forgets
--     them, so the risk_halts counter is kept aside and written back: a
--     halt id is never reused.

CREATE TEMP TABLE _seq_038 AS
    SELECT name, seq FROM sqlite_sequence WHERE name = 'risk_halts';
CREATE TEMP TABLE _reports_038 AS SELECT * FROM reconcile_reports;

DROP INDEX IF EXISTS idx_reconcile_reports_portfolio;
DROP TABLE reconcile_reports;

-- ---- risk_halts -----------------------------------------------------------
DROP TRIGGER IF EXISTS risk_halts_no_delete;
DROP TRIGGER IF EXISTS risk_halts_clear_only;
DROP INDEX IF EXISTS idx_risk_halts_open;
DROP INDEX IF EXISTS idx_risk_halts_portfolio;

CREATE TABLE risk_halts_038 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('month_loss', 'week_loss', 'drawdown', 'operational', 'kill',
                                 'runaway', 'broker_drift', 'intraday_loss')),
    scope        TEXT NOT NULL CHECK (scope IN ('global', 'user', 'portfolio')),
    user_id      TEXT,
    portfolio_id TEXT,
    halt         TEXT NOT NULL DEFAULT 'buys' CHECK (halt IN ('buys', 'all')),
    reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    tripped_by   TEXT NOT NULL CHECK (length(trim(tripped_by)) > 0),
    tripped_at   TEXT NOT NULL,
    expires_on   TEXT,
    cleared_at   TEXT,
    cleared_by   TEXT,
    clear_reason TEXT,
    CHECK (
        (scope = 'global' AND user_id IS NULL AND portfolio_id IS NULL)
        OR (scope = 'user' AND user_id IS NOT NULL AND portfolio_id IS NULL)
        OR (scope = 'portfolio' AND portfolio_id IS NOT NULL)
    ),
    CHECK (
        (cleared_at IS NULL AND cleared_by IS NULL AND clear_reason IS NULL)
        OR (cleared_at IS NOT NULL AND length(trim(cleared_by)) > 0
            AND length(trim(clear_reason)) > 0)
    )
);

INSERT INTO risk_halts_038 (id, kind, scope, user_id, portfolio_id, halt, reason, tripped_by,
    tripped_at, expires_on, cleared_at, cleared_by, clear_reason)
SELECT id, kind, scope, user_id, portfolio_id, halt, reason, tripped_by, tripped_at,
    expires_on, cleared_at, cleared_by, clear_reason
FROM risk_halts;

DROP TABLE risk_halts;
ALTER TABLE risk_halts_038 RENAME TO risk_halts;

CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_halts_open
    ON risk_halts(kind, scope, COALESCE(user_id, ''), COALESCE(portfolio_id, ''))
    WHERE cleared_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_risk_halts_portfolio ON risk_halts(portfolio_id, id);

CREATE TRIGGER IF NOT EXISTS risk_halts_no_delete
BEFORE DELETE ON risk_halts
BEGIN
    SELECT RAISE(ABORT, 'risk_halts is append-only');
END;

CREATE TRIGGER IF NOT EXISTS risk_halts_clear_only
BEFORE UPDATE ON risk_halts
WHEN OLD.cleared_at IS NOT NULL
  OR NEW.kind IS NOT OLD.kind
  OR NEW.scope IS NOT OLD.scope
  OR NEW.user_id IS NOT OLD.user_id
  OR NEW.portfolio_id IS NOT OLD.portfolio_id
  OR NEW.halt IS NOT OLD.halt
  OR NEW.reason IS NOT OLD.reason
  OR NEW.tripped_by IS NOT OLD.tripped_by
  OR NEW.tripped_at IS NOT OLD.tripped_at
  OR NEW.expires_on IS NOT OLD.expires_on
BEGIN
    SELECT RAISE(ABORT, 'a risk halt can only be cleared, once');
END;

-- ---- reconcile_reports (unchanged, rebuilt only because risk_halts was) ----
CREATE TABLE reconcile_reports (
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

INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status, items_json,
    explained_json, external_json, summary_json, detail, halt_id, paused_json)
SELECT id, portfolio_id, kind, as_of, taken_at, status, items_json, explained_json,
    external_json, summary_json, detail, halt_id, paused_json
FROM _reports_038;

CREATE INDEX IF NOT EXISTS idx_reconcile_reports_portfolio
    ON reconcile_reports(portfolio_id, taken_at DESC);

-- ---- AUTOINCREMENT counter ------------------------------------------------
DELETE FROM sqlite_sequence WHERE name = 'risk_halts';
INSERT INTO sqlite_sequence (name, seq) SELECT name, seq FROM _seq_038;

DROP TABLE _seq_038;
DROP TABLE _reports_038;
