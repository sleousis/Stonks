-- Accounts data model (roadmap 15.2, step S1 of docs/design/accounts-and-modes.md).
--
-- Market facts and the strategy catalog stay global; money, people and
-- delivery become scoped. Portfolio-scoped rows carry portfolio_id,
-- user-scoped rows user_id; a portfolio's owner is always looked up through
-- portfolios.owner_id, never copied. Repositories in stonks.accounts are the
-- only writers of the new tables.
--
-- Upgrade of an existing single-owner install: one admin (usr_owner) and one
-- simulated portfolio (pf_default) are created, every existing order, fill
-- and snapshot is assigned to pf_default, jobs and drafts to usr_owner, and
-- pf_default is subscribed (paper, equal weight) to every active strategy.
--
-- Writers that don't know portfolio_id yet (today's tick and reconcile)
-- keep working: while exactly one portfolio exists, a row inserted without
-- portfolio_id is assigned to it. With two or more portfolios such an
-- insert is refused, so a forgotten scope fails closed instead of landing in
-- someone's book.

-- ---- users ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,                   -- usr_<hex>
    kind            TEXT NOT NULL DEFAULT 'human' CHECK (kind IN ('human', 'service')),
    email           TEXT COLLATE NOCASE UNIQUE,         -- NULL until bootstrap sets it
    display_name    TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    role            TEXT NOT NULL CHECK (role IN ('viewer', 'trader', 'admin')),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    password_hash   TEXT,                               -- argon2id, step S2
    totp_secret_enc TEXT,                               -- sealed TOTP secret, step S2
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    created_at      TEXT NOT NULL,                      -- ISO-8601 UTC
    last_login_at   TEXT,
    -- Service accounts act in-process: no password, no second factor.
    CHECK (kind = 'human' OR (password_hash IS NULL AND totp_secret_enc IS NULL))
);

-- ---- portfolios -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolios (
    id                   TEXT PRIMARY KEY,              -- pf_<hex>
    owner_id             TEXT NOT NULL REFERENCES users(id),
    name                 TEXT NOT NULL CHECK (length(trim(name)) > 0),
    kind                 TEXT NOT NULL CHECK (kind IN ('simulated', 'broker')),
    broker_connection_id TEXT,                          -- broker_connections (step S3)
    external_account_id  TEXT,
    base_currency        TEXT NOT NULL DEFAULT 'USD',
    initial_cash         REAL CHECK (initial_cash IS NULL OR initial_cash >= 0),  -- NULL = [production].initial_cash
    allow_short          INTEGER NOT NULL DEFAULT 0 CHECK (allow_short IN (0, 1)),
    universe             TEXT,                          -- JSON list; NULL = global universe
    risk_policy_json     TEXT NOT NULL DEFAULT '{}',    -- partial RiskPolicy, tighten only
    construction_json    TEXT NOT NULL DEFAULT '{}',    -- partial construction settings
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'paused', 'archived')),
    created_at           TEXT NOT NULL,
    UNIQUE (owner_id, name)
);

CREATE INDEX IF NOT EXISTS idx_portfolios_owner ON portfolios(owner_id);

-- One external account links to at most one portfolio, so client ids and
-- reconciliation never collide.
CREATE UNIQUE INDEX IF NOT EXISTS ux_portfolios_external_account
    ON portfolios(broker_connection_id, external_account_id)
    WHERE external_account_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS portfolios_owner_immutable
BEFORE UPDATE OF owner_id ON portfolios
WHEN NEW.owner_id IS NOT OLD.owner_id
BEGIN
    SELECT RAISE(ABORT, 'portfolios.owner_id is immutable');
END;

-- ---- subscriptions --------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
    id                   TEXT PRIMARY KEY,              -- sub_<hex>
    user_id              TEXT NOT NULL REFERENCES users(id),
    strategy_id          TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    portfolio_id         TEXT REFERENCES portfolios(id),  -- NULL only for notify
    mode                 TEXT NOT NULL CHECK (mode IN ('notify', 'paper', 'auto')),
    weight               REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),  -- share of the risk budget
    risk_overrides_json  TEXT NOT NULL DEFAULT '{}',    -- partial RiskPolicy for this slice
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    -- Auto gate (decision 2026-09-26): trading days completed in paper mode
    -- on this subscription without breaking its risk limits. A breach resets
    -- it; paper_last_as_of makes the daily increment idempotent.
    paper_days_completed INTEGER NOT NULL DEFAULT 0 CHECK (paper_days_completed >= 0),
    paper_last_as_of     TEXT,
    auto_enabled_at      TEXT,
    auto_enabled_by      TEXT,                          -- actor
    paused_reason        TEXT,                          -- set = auto_paused
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (mode = 'notify' OR portfolio_id IS NOT NULL)
);

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

-- ---- audit_log ------------------------------------------------------------
-- Every user action (mode changes, portfolio changes, cross-user admin
-- reads). Strategy lifecycle stays in status_changes.
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    actor        TEXT NOT NULL CHECK (length(trim(actor)) > 0),  -- "user:<id>" | "service:<name>"
    action       TEXT NOT NULL CHECK (length(trim(action)) > 0), -- e.g. subscription.mode
    target_kind  TEXT NOT NULL,
    target_id    TEXT,
    portfolio_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    ip           TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_portfolio ON audit_log(portfolio_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor, id);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- ---- default owner and portfolio ------------------------------------------
INSERT OR IGNORE INTO users (id, kind, email, display_name, role, status, created_at)
VALUES ('usr_owner', 'human', NULL, 'Owner', 'admin', 'active',
        strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

-- kind = 'simulated': a migration can't read [brokers].kind. An Alpaca
-- install turns pf_default into kind = 'broker' when step S3's
-- `stonks connections import-env` creates its connection row.
INSERT OR IGNORE INTO portfolios (id, owner_id, name, kind, created_at)
VALUES ('pf_default', 'usr_owner', 'Default', 'simulated',
        strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

-- ---- portfolio_id on orders, fills, portfolio_snapshots --------------------
-- SQLite can't add a NOT NULL REFERENCES column; the triggers below enforce
-- presence, existence and immutability instead.
ALTER TABLE orders ADD COLUMN portfolio_id TEXT;
ALTER TABLE fills ADD COLUMN portfolio_id TEXT;
ALTER TABLE portfolio_snapshots ADD COLUMN portfolio_id TEXT;

UPDATE orders SET portfolio_id = 'pf_default' WHERE portfolio_id IS NULL;
UPDATE fills SET portfolio_id = 'pf_default' WHERE portfolio_id IS NULL;
UPDATE portfolio_snapshots SET portfolio_id = 'pf_default' WHERE portfolio_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_orders_portfolio ON orders(portfolio_id, created_at);
CREATE INDEX IF NOT EXISTS idx_fills_portfolio ON fills(portfolio_id, filled_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_portfolio ON portfolio_snapshots(portfolio_id, id);

-- orders
CREATE TRIGGER IF NOT EXISTS orders_portfolio_check
BEFORE INSERT ON orders
BEGIN
    SELECT RAISE(ABORT, 'orders.portfolio_id is required')
     WHERE NEW.portfolio_id IS NULL AND (SELECT COUNT(*) FROM portfolios) <> 1;
    SELECT RAISE(ABORT, 'orders.portfolio_id references an unknown portfolio')
     WHERE NEW.portfolio_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM portfolios WHERE id = NEW.portfolio_id);
END;

CREATE TRIGGER IF NOT EXISTS orders_portfolio_legacy_fill
AFTER INSERT ON orders
WHEN NEW.portfolio_id IS NULL
BEGIN
    UPDATE orders SET portfolio_id = (SELECT id FROM portfolios) WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS orders_portfolio_immutable
BEFORE UPDATE OF portfolio_id ON orders
WHEN OLD.portfolio_id IS NOT NULL AND NEW.portfolio_id IS NOT OLD.portfolio_id
BEGIN
    SELECT RAISE(ABORT, 'orders.portfolio_id is immutable');
END;

-- fills: a fill belongs to its order's portfolio.
CREATE TRIGGER IF NOT EXISTS fills_portfolio_check
BEFORE INSERT ON fills
BEGIN
    SELECT RAISE(ABORT, 'fills.portfolio_id is required')
     WHERE NEW.portfolio_id IS NULL
       AND (SELECT portfolio_id FROM orders WHERE client_id = NEW.order_client_id) IS NULL
       AND (SELECT COUNT(*) FROM portfolios) <> 1;
    SELECT RAISE(ABORT, 'fills.portfolio_id must be its order''s portfolio')
     WHERE NEW.portfolio_id IS NOT NULL
       AND NEW.portfolio_id IS NOT
           (SELECT portfolio_id FROM orders WHERE client_id = NEW.order_client_id);
END;

CREATE TRIGGER IF NOT EXISTS fills_portfolio_legacy_fill
AFTER INSERT ON fills
WHEN NEW.portfolio_id IS NULL
BEGIN
    UPDATE fills
       SET portfolio_id = COALESCE(
               (SELECT portfolio_id FROM orders WHERE client_id = NEW.order_client_id),
               (SELECT id FROM portfolios))
     WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS fills_portfolio_immutable
BEFORE UPDATE OF portfolio_id ON fills
WHEN OLD.portfolio_id IS NOT NULL AND NEW.portfolio_id IS NOT OLD.portfolio_id
BEGIN
    SELECT RAISE(ABORT, 'fills.portfolio_id is immutable');
END;

-- portfolio_snapshots
CREATE TRIGGER IF NOT EXISTS snapshots_portfolio_check
BEFORE INSERT ON portfolio_snapshots
BEGIN
    SELECT RAISE(ABORT, 'portfolio_snapshots.portfolio_id is required')
     WHERE NEW.portfolio_id IS NULL AND (SELECT COUNT(*) FROM portfolios) <> 1;
    SELECT RAISE(ABORT, 'portfolio_snapshots.portfolio_id references an unknown portfolio')
     WHERE NEW.portfolio_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM portfolios WHERE id = NEW.portfolio_id);
END;

CREATE TRIGGER IF NOT EXISTS snapshots_portfolio_legacy_fill
AFTER INSERT ON portfolio_snapshots
WHEN NEW.portfolio_id IS NULL
BEGIN
    UPDATE portfolio_snapshots SET portfolio_id = (SELECT id FROM portfolios)
     WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS snapshots_portfolio_immutable
BEFORE UPDATE OF portfolio_id ON portfolio_snapshots
WHEN OLD.portfolio_id IS NOT NULL AND NEW.portfolio_id IS NOT OLD.portfolio_id
BEGIN
    SELECT RAISE(ABORT, 'portfolio_snapshots.portfolio_id is immutable');
END;

-- ---- owner_id on jobs and strategy_drafts (global, attributed) -------------
ALTER TABLE jobs ADD COLUMN owner_id TEXT;
ALTER TABLE strategy_drafts ADD COLUMN owner_id TEXT;

UPDATE jobs SET owner_id = 'usr_owner' WHERE owner_id IS NULL;
UPDATE strategy_drafts SET owner_id = 'usr_owner' WHERE owner_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_strategy_drafts_owner ON strategy_drafts(owner_id);

-- Attribution for writers that don't pass an owner yet: the sole human user,
-- if there is exactly one; otherwise the row stays unattributed (NULL).
CREATE TRIGGER IF NOT EXISTS jobs_owner_legacy_fill
AFTER INSERT ON jobs
WHEN NEW.owner_id IS NULL AND (SELECT COUNT(*) FROM users WHERE kind = 'human') = 1
BEGIN
    UPDATE jobs SET owner_id = (SELECT id FROM users WHERE kind = 'human')
     WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS strategy_drafts_owner_legacy_fill
AFTER INSERT ON strategy_drafts
WHEN NEW.owner_id IS NULL AND (SELECT COUNT(*) FROM users WHERE kind = 'human') = 1
BEGIN
    UPDATE strategy_drafts SET owner_id = (SELECT id FROM users WHERE kind = 'human')
     WHERE rowid = NEW.rowid;
END;

-- ---- alerts become user-scoped notifications -------------------------------
-- user_id NULL = audience "admins" (today's behaviour).
ALTER TABLE alerts ADD COLUMN user_id TEXT REFERENCES users(id);
ALTER TABLE alerts ADD COLUMN category TEXT
    CHECK (category IS NULL OR category IN ('signal', 'order', 'risk', 'system'));
ALTER TABLE alerts ADD COLUMN dedupe_key TEXT;
ALTER TABLE alerts ADD COLUMN read_at TEXT;

CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, id);

-- ---- one paper subscription per active strategy ---------------------------
INSERT INTO subscriptions
    (id, user_id, strategy_id, portfolio_id, mode, weight, created_at, updated_at)
SELECT 'sub_' || lower(hex(randomblob(8))), 'usr_owner', s.id, 'pf_default', 'paper', 1.0,
       strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
  FROM strategies s
 WHERE s.status = 'active'
   AND NOT EXISTS (
       SELECT 1 FROM subscriptions x
        WHERE x.portfolio_id = 'pf_default' AND x.strategy_id = s.id
   );
