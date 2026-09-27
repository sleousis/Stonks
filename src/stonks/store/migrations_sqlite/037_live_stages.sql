-- Live stages, gates and preview (roadmap 19.9, design
-- docs/design/live-trading.md section 1 "Stages and gates").
--
-- portfolios.live_stage   where a portfolio stands on the way to real money:
--                         sim_paper (simulated, the default), broker_paper
--                         (the broker's paper account), live_small (real
--                         money, a small allocation), live_scale (the owner
--                         raised the allocation by hand).
-- live_stage_changes      append-only, like status_changes. Every stage
--                         change writes one row first, with the gate report
--                         computed at that moment for a promotion.
-- live_gate_days          the gate metrics of one portfolio for one session,
--                         written after the tick by the live_gate_days job.

ALTER TABLE portfolios ADD COLUMN live_stage TEXT NOT NULL DEFAULT 'sim_paper'
    CHECK (live_stage IN ('sim_paper', 'broker_paper', 'live_small', 'live_scale'));

CREATE TABLE IF NOT EXISTS live_stage_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id     TEXT NOT NULL REFERENCES portfolios(id),
    from_stage       TEXT NOT NULL
                     CHECK (from_stage IN ('sim_paper', 'broker_paper', 'live_small', 'live_scale')),
    to_stage         TEXT NOT NULL
                     CHECK (to_stage IN ('sim_paper', 'broker_paper', 'live_small', 'live_scale')),
    direction        TEXT NOT NULL CHECK (direction IN ('promote', 'demote')),
    actor            TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    reason           TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    -- JSON GateReport for a promotion, NULL for a demotion
    gate_report_json TEXT,
    created_at       TEXT NOT NULL,
    CHECK (from_stage <> to_stage),
    CHECK (direction = 'demote' OR gate_report_json IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_live_stage_changes_portfolio
    ON live_stage_changes(portfolio_id, id);

CREATE TRIGGER IF NOT EXISTS live_stage_changes_no_update
BEFORE UPDATE ON live_stage_changes
BEGIN
    SELECT RAISE(ABORT, 'live_stage_changes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS live_stage_changes_no_delete
BEFORE DELETE ON live_stage_changes
BEGIN
    SELECT RAISE(ABORT, 'live_stage_changes is append-only');
END;

-- No unaudited stage change: production.live.stages logs the change first,
-- then moves the portfolio. Any other write has no matching latest row.
CREATE TRIGGER IF NOT EXISTS portfolios_live_stage_audited
BEFORE UPDATE OF live_stage ON portfolios
WHEN NEW.live_stage IS NOT OLD.live_stage AND NOT EXISTS (
    SELECT 1 FROM live_stage_changes c
    WHERE c.id = (SELECT MAX(id) FROM live_stage_changes WHERE portfolio_id = NEW.id)
      AND c.from_stage = OLD.live_stage
      AND c.to_stage = NEW.live_stage
)
BEGIN
    SELECT RAISE(ABORT, 'a live stage change needs a live_stage_changes row');
END;

CREATE TABLE IF NOT EXISTS live_gate_days (
    portfolio_id             TEXT NOT NULL REFERENCES portfolios(id),
    session_date             TEXT NOT NULL,
    stage                    TEXT NOT NULL
                             CHECK (stage IN ('sim_paper', 'broker_paper', 'live_small',
                                              'live_scale')),
    -- orders created that session: sent to the broker, filled (in part or
    -- whole), rejected by the broker, refused by our own rules, and not
    -- terminal when the metrics were taken
    orders_sent              INTEGER NOT NULL DEFAULT 0 CHECK (orders_sent >= 0),
    orders_filled            INTEGER NOT NULL DEFAULT 0 CHECK (orders_filled >= 0),
    orders_rejected          INTEGER NOT NULL DEFAULT 0 CHECK (orders_rejected >= 0),
    orders_refused           INTEGER NOT NULL DEFAULT 0 CHECK (orders_refused >= 0),
    stuck_orders             INTEGER NOT NULL DEFAULT 0 CHECK (stuck_orders >= 0),
    fills                    INTEGER NOT NULL DEFAULT 0 CHECK (fills >= 0),
    fills_missing_commission INTEGER NOT NULL DEFAULT 0 CHECK (fills_missing_commission >= 0),
    -- fill quality: realised shortfall minus the cost model's estimate
    tca_orders               INTEGER NOT NULL DEFAULT 0 CHECK (tca_orders >= 0),
    tca_gap_bps              REAL,
    -- tracking against the model book (NULL when either side has no return)
    live_return              REAL,
    model_return             REAL,
    -- unexplained reconciliation items (NULL: no reconcile report to read)
    drift_items              INTEGER CHECK (drift_items IS NULL OR drift_items >= 0),
    clean                    INTEGER NOT NULL CHECK (clean IN (0, 1)),
    computed_at              TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, session_date)
);
