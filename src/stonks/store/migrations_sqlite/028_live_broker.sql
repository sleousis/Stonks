-- Go live with real money, wave 1 (roadmap 19.1, 19.4, 19.6, 19.7;
-- design docs/design/live-trading.md).
--
-- 19.1 broker seam
--   orders.broker_ref       the reference sent to the broker (the client id,
--                           or a stable hash when the broker keeps fewer
--                           characters). Idempotency at brokers with no
--                           server-side dedupe.
--   orders.stop_price, time_in_force, outside_rth   the live order fields.
--   fills.broker_exec_id    one fill per broker execution. Unique per
--                           portfolio, so booking an execution twice is a
--                           no-op and a late commission updates the fee.
--   fills.fee_currency, fee_fx_rate   a commission in another currency.
--   orders.state            the fine order state (execution.order_state):
--                           submitted, accepted, pending_cancel, expired and
--                           unknown besides the five statuses. NULL on older
--                           rows, which read it from status. status follows it.

ALTER TABLE orders ADD COLUMN broker_ref TEXT;
ALTER TABLE orders ADD COLUMN stop_price REAL CHECK (stop_price IS NULL OR stop_price > 0);
ALTER TABLE orders ADD COLUMN time_in_force TEXT
    CHECK (time_in_force IS NULL OR time_in_force IN ('day', 'gtc', 'opg', 'ioc'));
ALTER TABLE orders ADD COLUMN outside_rth INTEGER NOT NULL DEFAULT 0
    CHECK (outside_rth IN (0, 1));
ALTER TABLE orders ADD COLUMN state TEXT
    CHECK (state IS NULL OR state IN ('pending', 'submitted', 'accepted', 'partially_filled',
                                      'filled', 'pending_cancel', 'cancelled', 'expired',
                                      'rejected', 'unknown'));

CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state) WHERE state IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_broker_ref
    ON orders(COALESCE(portfolio_id, ''), broker_ref) WHERE broker_ref IS NOT NULL;

ALTER TABLE fills ADD COLUMN broker_exec_id TEXT;
ALTER TABLE fills ADD COLUMN fee_currency TEXT
    CHECK (fee_currency IS NULL OR length(fee_currency) = 3);
ALTER TABLE fills ADD COLUMN fee_fx_rate REAL CHECK (fee_fx_rate IS NULL OR fee_fx_rate > 0);

CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_broker_exec
    ON fills(COALESCE(portfolio_id, ''), broker_exec_id) WHERE broker_exec_id IS NOT NULL;

-- 19.6 halt kinds: risk_halts gains 'runaway' (a run that tries to close
-- more positions than the ceiling) and 'broker_drift' (reconciliation found
-- a difference it cannot explain, roadmap 19.5). SQLite cannot change a
-- CHECK, so the table is rebuilt with its rows, indexes and triggers.

DROP TRIGGER IF EXISTS risk_halts_no_delete;
DROP TRIGGER IF EXISTS risk_halts_clear_only;
DROP INDEX IF EXISTS idx_risk_halts_open;
DROP INDEX IF EXISTS idx_risk_halts_portfolio;

CREATE TABLE risk_halts_028 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('month_loss', 'week_loss', 'drawdown', 'operational', 'kill',
                                 'runaway', 'broker_drift')),
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

INSERT INTO risk_halts_028 SELECT * FROM risk_halts;
DROP TABLE risk_halts;
ALTER TABLE risk_halts_028 RENAME TO risk_halts;

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

-- 19.6 live allocation: the amount the owner lets Stonks trade in a live
-- portfolio (the capital_ramp rule caps the book's gross exposure at it).
-- Set by hand only, with a fresh second factor. Every change is also an
-- audit_log row. No row means nothing may open.

CREATE TABLE IF NOT EXISTS live_allocations (
    portfolio_id TEXT PRIMARY KEY REFERENCES portfolios(id),
    amount       REAL NOT NULL CHECK (amount >= 0),
    currency     TEXT NOT NULL CHECK (length(currency) = 3),
    reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    updated_at   TEXT NOT NULL,
    updated_by   TEXT NOT NULL CHECK (length(trim(updated_by)) > 0)
);

-- 19.4 gateway health: the last broker health check per gateway. The
-- broker_health job writes it every few minutes, and a gateway down for
-- too many sessions pauses the auto subscriptions of its portfolios.

CREATE TABLE IF NOT EXISTS broker_gateway_status (
    gateway              TEXT PRIMARY KEY,
    mode                 TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    connected            INTEGER NOT NULL CHECK (connected IN (0, 1)),
    last_check_at        TEXT NOT NULL,
    last_ok_at           TEXT,
    down_since           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    -- a real fault (login refused, wrong account, competing session), or NULL
    fault                TEXT,
    detail               TEXT,
    latency_ms           REAL,
    alerted_at           TEXT,
    paused_at            TEXT
);

-- 19.7 account rules. One profile per live portfolio: where the account is
-- held (the broker entity, not the owner's passport), cash or margin, and
-- retail or professional.

CREATE TABLE IF NOT EXISTS account_profiles (
    portfolio_id   TEXT PRIMARY KEY REFERENCES portfolios(id),
    jurisdiction   TEXT NOT NULL CHECK (jurisdiction IN ('us', 'eu', 'uk')),
    account_type   TEXT NOT NULL DEFAULT 'cash' CHECK (account_type IN ('cash', 'margin')),
    client_class   TEXT NOT NULL DEFAULT 'retail'
                   CHECK (client_class IN ('retail', 'professional')),
    base_currency  TEXT NOT NULL DEFAULT 'USD' CHECK (length(base_currency) = 3),
    fx_policy      TEXT NOT NULL DEFAULT 'refuse' CHECK (fx_policy IN ('refuse', 'convert')),
    wash_sale_mode TEXT NOT NULL DEFAULT 'warn' CHECK (wash_sale_mode IN ('warn', 'block')),
    allow_short    INTEGER NOT NULL DEFAULT 0 CHECK (allow_short IN (0, 1)),
    updated_at     TEXT NOT NULL,
    updated_by     TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    -- shorts need a margin account
    CHECK (allow_short = 0 OR account_type = 'margin')
);

-- The settlement date of each fill's cash, from the security's market.
CREATE TABLE IF NOT EXISTS settlement_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL,
    fill_id      INTEGER NOT NULL UNIQUE REFERENCES fills(id),
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    currency     TEXT NOT NULL CHECK (length(currency) = 3),
    -- signed cash: negative for a buy, positive for a sell
    amount       REAL NOT NULL,
    trade_date   TEXT NOT NULL,
    settle_date  TEXT NOT NULL CHECK (settle_date >= trade_date)
);

CREATE INDEX IF NOT EXISTS idx_settlement_ledger_portfolio
    ON settlement_ledger(portfolio_id, settle_date);

-- Tickers a live portfolio must not buy: the owner's own list and names the
-- broker refused earlier.
CREATE TABLE IF NOT EXISTS account_restricted (
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    ticker       TEXT NOT NULL,
    reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    source       TEXT NOT NULL CHECK (source IN ('owner', 'broker')),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, ticker)
);

-- Whether a fund has the key information document a retail client in the
-- EU or UK needs (PRIIPs and its UK successor). Filled from broker
-- rejections and an owner list.
CREATE TABLE IF NOT EXISTS product_documents (
    ticker        TEXT NOT NULL,
    jurisdiction  TEXT NOT NULL CHECK (jurisdiction IN ('eu', 'uk')),
    kid_available INTEGER NOT NULL CHECK (kid_available IN (0, 1)),
    source        TEXT NOT NULL CHECK (source IN ('owner', 'broker')),
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (ticker, jurisdiction)
);
