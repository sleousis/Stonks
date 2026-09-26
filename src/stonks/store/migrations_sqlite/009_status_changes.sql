-- Intervention log (BL-24): one append-only row per human or automated
-- change to a strategy's status, plus other logged interventions (risk
-- resets, manual orders, config changes). Written only by
-- StrategyRegistry, in the same transaction as the change it records.
CREATE TABLE IF NOT EXISTS status_changes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id        TEXT,                -- NULL for book-wide interventions
    kind               TEXT NOT NULL DEFAULT 'status'
                       CHECK (kind IN ('status', 'risk_reset', 'manual_order', 'config')),
    from_status        TEXT,
    to_status          TEXT,
    actor              TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    reason             TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    override           INTEGER NOT NULL DEFAULT 0 CHECK (override IN (0, 1)),
    golive_passed      INTEGER CHECK (golive_passed IN (0, 1)),  -- NULL: no report
    golive_report_json TEXT,                                     -- JSON GoLiveReport
    created_at         TEXT NOT NULL,                            -- ISO-8601 UTC
    CHECK (kind <> 'status' OR (strategy_id IS NOT NULL AND to_status IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_status_changes_strategy ON status_changes(strategy_id, id);

CREATE TRIGGER IF NOT EXISTS status_changes_no_update
BEFORE UPDATE ON status_changes
BEGIN
    SELECT RAISE(ABORT, 'status_changes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS status_changes_no_delete
BEFORE DELETE ON status_changes
BEGIN
    SELECT RAISE(ABORT, 'status_changes is append-only');
END;

-- No unaudited status change: set_status logs the change first, then updates
-- the strategy with updated_at = the log row's created_at. Any other status
-- write (raw SQL, a replayed old change) has no matching latest row.
CREATE TRIGGER IF NOT EXISTS strategies_status_audited
BEFORE UPDATE OF status ON strategies
WHEN NEW.status IS NOT OLD.status AND NOT EXISTS (
    SELECT 1 FROM status_changes sc
    WHERE sc.id = (
        SELECT MAX(id) FROM status_changes WHERE strategy_id = NEW.id AND kind = 'status'
    )
      AND sc.from_status = OLD.status
      AND sc.to_status = NEW.status
      AND sc.created_at = NEW.updated_at
)
BEGIN
    SELECT RAISE(ABORT, 'status changes go through StrategyRegistry.set_status (audited)');
END;
