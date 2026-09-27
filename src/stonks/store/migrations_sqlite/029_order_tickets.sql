-- Tickets, approve mode and submit (roadmap 19.8, design
-- docs/design/live-trading.md section 4 "Approve mode").
--
-- subscriptions.mode gains 'approve': the tick decides and every order
-- waits for a person. SQLite cannot change a CHECK, so the table is rebuilt
-- with its rows, indexes and triggers. Dropping the old table fires the
-- ON DELETE SET NULL of position_attribution.subscription_id, so those ids
-- are kept aside first and written back after the rename.

CREATE TEMP TABLE _attribution_subs_029 AS
    SELECT id, subscription_id FROM position_attribution WHERE subscription_id IS NOT NULL;

DROP TRIGGER IF EXISTS subscriptions_owner_insert;
DROP TRIGGER IF EXISTS subscriptions_owner_update;
DROP INDEX IF EXISTS ux_subscriptions_portfolio_strategy;
DROP INDEX IF EXISTS ux_subscriptions_user_strategy_notify;
DROP INDEX IF EXISTS idx_subscriptions_user;
DROP INDEX IF EXISTS idx_subscriptions_strategy;

CREATE TABLE subscriptions_029 (
    id                   TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL REFERENCES users(id),
    strategy_id          TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    portfolio_id         TEXT REFERENCES portfolios(id),
    mode                 TEXT NOT NULL CHECK (mode IN ('notify', 'paper', 'approve', 'auto')),
    weight               REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
    risk_overrides_json  TEXT NOT NULL DEFAULT '{}',
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    paper_days_completed INTEGER NOT NULL DEFAULT 0 CHECK (paper_days_completed >= 0),
    paper_last_as_of     TEXT,
    auto_enabled_at      TEXT,
    auto_enabled_by      TEXT,
    paused_reason        TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    paper_since          TEXT,
    CHECK (mode = 'notify' OR portfolio_id IS NOT NULL)
);

INSERT INTO subscriptions_029 (id, user_id, strategy_id, portfolio_id, mode, weight,
    risk_overrides_json, enabled, paper_days_completed, paper_last_as_of, auto_enabled_at,
    auto_enabled_by, paused_reason, created_at, updated_at, paper_since)
SELECT id, user_id, strategy_id, portfolio_id, mode, weight, risk_overrides_json, enabled,
    paper_days_completed, paper_last_as_of, auto_enabled_at, auto_enabled_by, paused_reason,
    created_at, updated_at, paper_since
FROM subscriptions;

DROP TABLE subscriptions;
ALTER TABLE subscriptions_029 RENAME TO subscriptions;

UPDATE position_attribution
   SET subscription_id = (SELECT a.subscription_id FROM _attribution_subs_029 a
                           WHERE a.id = position_attribution.id)
 WHERE id IN (SELECT id FROM _attribution_subs_029);
DROP TABLE _attribution_subs_029;

CREATE UNIQUE INDEX IF NOT EXISTS ux_subscriptions_portfolio_strategy
    ON subscriptions(portfolio_id, strategy_id) WHERE portfolio_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_subscriptions_user_strategy_notify
    ON subscriptions(user_id, strategy_id) WHERE portfolio_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_strategy ON subscriptions(strategy_id);

CREATE TRIGGER IF NOT EXISTS subscriptions_owner_insert
BEFORE INSERT ON subscriptions
WHEN NEW.portfolio_id IS NOT NULL
 AND NEW.user_id IS NOT (SELECT owner_id FROM portfolios WHERE id = NEW.portfolio_id)
BEGIN
    SELECT RAISE(ABORT, 'a subscription''s user must own the portfolio');
END;

CREATE TRIGGER IF NOT EXISTS subscriptions_owner_update
BEFORE UPDATE OF user_id, portfolio_id ON subscriptions
WHEN NEW.portfolio_id IS NOT NULL
 AND NEW.user_id IS NOT (SELECT owner_id FROM portfolios WHERE id = NEW.portfolio_id)
BEGIN
    SELECT RAISE(ABORT, 'a subscription''s user must own the portfolio');
END;

-- order_tickets: one per order a live book decided (production.tickets).
-- The tick writes them after the close. Auto books write them already
-- approved by service:system. Approve books, and every close of a runaway
-- run, wait for a person (hold). The live_submit job sends approved
-- tickets in the submit window (submit_after to expires_at) as orders with
-- the ticket's client id, so a ticket is idempotent like any order.
-- Append-only: tickets are the audit trail of what was sent and why.

CREATE TABLE IF NOT EXISTS order_tickets (
    id              TEXT PRIMARY KEY,                     -- tkt_<hex>
    portfolio_id    TEXT NOT NULL REFERENCES portfolios(id),
    tick_id         TEXT,
    as_of           TEXT NOT NULL,                        -- the decision day
    client_id       TEXT NOT NULL UNIQUE,                 -- the order it becomes
    strategy_id     TEXT,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    limit_price     REAL CHECK (limit_price IS NULL OR limit_price > 0),
    order_json      TEXT NOT NULL,                        -- the full order as decided
    preview_json    TEXT NOT NULL DEFAULT '{}',           -- reference price, notional, what-if
    reason_json     TEXT NOT NULL DEFAULT '{}',           -- signal, score, weight, rules
    hold            TEXT CHECK (hold IS NULL OR hold IN ('approve_mode', 'runaway')),
    status          TEXT NOT NULL
                    CHECK (status IN ('awaiting_approval', 'approved', 'rejected', 'expired',
                                      'submitted', 'filled', 'unfilled', 'cancelled',
                                      'failed')),
    submit_after    TEXT NOT NULL,                        -- the submit window opens
    expires_at      TEXT NOT NULL,                        -- the submit deadline
    decided_by      TEXT,                                 -- actor
    decided_at      TEXT,
    decision_reason TEXT,
    submitted_at    TEXT,
    status_reason   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (status <> 'rejected' OR length(trim(COALESCE(decision_reason, ''))) > 0)
);

CREATE INDEX IF NOT EXISTS idx_order_tickets_portfolio ON order_tickets(portfolio_id, status);
CREATE INDEX IF NOT EXISTS idx_order_tickets_due ON order_tickets(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_order_tickets_tick ON order_tickets(tick_id);

CREATE TRIGGER IF NOT EXISTS order_tickets_no_delete
BEFORE DELETE ON order_tickets
BEGIN
    SELECT RAISE(ABORT, 'order_tickets is append-only');
END;
