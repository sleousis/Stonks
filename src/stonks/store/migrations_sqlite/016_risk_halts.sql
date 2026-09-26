-- Risk halts and the kill switch (BL-28 W3.2, roadmap 12.6; design
-- docs/design/accounts-and-modes.md section 9).
--
-- One row per halt. While a row is open (cleared_at IS NULL) and not past
-- expires_on, the tick's risk_halts trade gate blocks new orders of the
-- portfolios it covers:
--
--   scope 'global'     every portfolio            (user_id, portfolio_id NULL)
--   scope 'user'       every portfolio of user_id (portfolio_id NULL)
--   scope 'portfolio'  portfolio_id only
--
-- halt 'buys' drops buys and lets sells and exits through; 'all' places
-- nothing. kind:
--   month_loss, week_loss  circuit breaker, expire at expires_on
--   drawdown               circuit breaker, latched until a person clears it
--   operational            stale data or a stuck run (from health)
--   kill                   the kill switch, until resumed with a typed confirmation
--
-- Rows are never deleted; the only change allowed is clearing an open row
-- once (cleared_at, cleared_by, clear_reason). Clears also write a
-- status_changes row of kind 'risk_reset'.

CREATE TABLE IF NOT EXISTS risk_halts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('month_loss', 'week_loss', 'drawdown', 'operational', 'kill')),
    scope        TEXT NOT NULL CHECK (scope IN ('global', 'user', 'portfolio')),
    user_id      TEXT,
    portfolio_id TEXT,
    halt         TEXT NOT NULL DEFAULT 'buys' CHECK (halt IN ('buys', 'all')),
    reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    tripped_by   TEXT NOT NULL CHECK (length(trim(tripped_by)) > 0),
    tripped_at   TEXT NOT NULL,                  -- ISO-8601 UTC
    expires_on   TEXT,                           -- ISO date; NULL: until cleared
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

-- At most one open halt of a kind per target.
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
