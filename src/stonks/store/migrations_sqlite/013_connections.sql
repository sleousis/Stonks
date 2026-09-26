-- Broker connections (roadmap 15.3, step S3 of docs/design/accounts-and-modes.md).
--
-- A connection is one user's link to a provider (SnapTrade, Alpaca, ...);
-- it can hold several external accounts, each linked to at most one
-- broker-kind portfolio of the same user. Credentials are sealed by
-- stonks.security.SecretBox; the master key never lives in this database.
-- stonks.connections is the only writer of these tables.
--
-- Syncs write portfolio_snapshots rows with source = 'sync' (one per
-- portfolio and day, updated in place by a re-sync), the full holdings of
-- that snapshot (unmapped symbols included, ticker NULL) in broker_positions,
-- and activities upserted by (connection_id, provider_activity_id).

-- ---- broker_connections -----------------------------------------------------
CREATE TABLE IF NOT EXISTS broker_connections (
    id                   TEXT PRIMARY KEY,              -- con_<hex>
    user_id              TEXT NOT NULL REFERENCES users(id),
    provider             TEXT NOT NULL,                 -- registry name (snaptrade, alpaca, ...)
    label                TEXT,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'active', 'error')),
    -- The provider-side user handle (SnapTrade userId). Not a secret.
    external_user_id     TEXT,
    -- Hosted-login callback: SHA-256 of the one-time state and its expiry.
    pending_state_hash   TEXT,
    pending_expires_at   TEXT,
    last_sync_at         TEXT,
    last_sync_status     TEXT CHECK (last_sync_status IS NULL
                                     OR last_sync_status IN ('ok', 'partial', 'error')),
    last_error           TEXT,                          -- redacted, never credentials
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    next_sync_at         TEXT,                          -- NULL = due now
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_broker_connections_user ON broker_connections(user_id);
CREATE INDEX IF NOT EXISTS idx_broker_connections_due
    ON broker_connections(status, next_sync_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_broker_connections_external_user
    ON broker_connections(provider, external_user_id) WHERE external_user_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS broker_connections_owner_immutable
BEFORE UPDATE OF user_id, provider ON broker_connections
WHEN NEW.user_id IS NOT OLD.user_id OR NEW.provider IS NOT OLD.provider
BEGIN
    SELECT RAISE(ABORT, 'broker_connections.user_id and provider are immutable');
END;

-- ---- broker_credentials -------------------------------------------------------
CREATE TABLE IF NOT EXISTS broker_credentials (
    connection_id TEXT PRIMARY KEY REFERENCES broker_connections(id) ON DELETE CASCADE,
    key_id        TEXT NOT NULL,                        -- master key that sealed it (rotation)
    ciphertext    TEXT NOT NULL,                        -- SecretBox token, bound to connection_id
    created_at    TEXT NOT NULL,
    rotated_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_broker_credentials_key ON broker_credentials(key_id);

-- ---- broker_accounts: what each connection exposes ------------------------------
CREATE TABLE IF NOT EXISTS broker_accounts (
    connection_id       TEXT NOT NULL REFERENCES broker_connections(id) ON DELETE CASCADE,
    external_account_id TEXT NOT NULL,
    name                TEXT NOT NULL,
    institution         TEXT,
    number_mask         TEXT,                           -- last 4 characters only
    currency            TEXT NOT NULL DEFAULT 'USD',
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    PRIMARY KEY (connection_id, external_account_id)
);

-- ---- a linked portfolio belongs to the connection's owner ---------------------
CREATE TRIGGER IF NOT EXISTS portfolios_connection_owner_insert
BEFORE INSERT ON portfolios
WHEN NEW.broker_connection_id IS NOT NULL
 AND NEW.owner_id IS NOT (SELECT user_id FROM broker_connections WHERE id = NEW.broker_connection_id)
BEGIN
    SELECT RAISE(ABORT, 'a linked portfolio must belong to the connection''s owner');
END;

CREATE TRIGGER IF NOT EXISTS portfolios_connection_owner_update
BEFORE UPDATE OF broker_connection_id, owner_id ON portfolios
WHEN NEW.broker_connection_id IS NOT NULL
 AND NEW.owner_id IS NOT (SELECT user_id FROM broker_connections WHERE id = NEW.broker_connection_id)
BEGIN
    SELECT RAISE(ABORT, 'a linked portfolio must belong to the connection''s owner');
END;

-- ---- snapshots: where a row came from ----------------------------------------
ALTER TABLE portfolio_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'tick'
    CHECK (source IN ('tick', 'sync'));

-- One sync snapshot per portfolio and day; a re-sync updates it in place.
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_sync_day
    ON portfolio_snapshots(portfolio_id, as_of) WHERE source = 'sync';

-- ---- broker_positions: every holding of a sync snapshot ------------------------
CREATE TABLE IF NOT EXISTS broker_positions (
    snapshot_id  INTEGER NOT NULL REFERENCES portfolio_snapshots(id) ON DELETE CASCADE,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    raw_symbol   TEXT NOT NULL,                         -- the provider's symbol
    ticker       TEXT,                                  -- NULL = not covered
    quantity     REAL NOT NULL,
    price        REAL,
    market_value REAL,
    currency     TEXT,
    description  TEXT,
    PRIMARY KEY (snapshot_id, raw_symbol)
);

CREATE INDEX IF NOT EXISTS idx_broker_positions_portfolio ON broker_positions(portfolio_id);

-- ---- broker_activities ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS broker_activities (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id        TEXT NOT NULL REFERENCES broker_connections(id) ON DELETE CASCADE,
    portfolio_id         TEXT REFERENCES portfolios(id),
    external_account_id  TEXT NOT NULL,
    provider_activity_id TEXT NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('trade', 'dividend', 'interest', 'fee',
                                                       'deposit', 'withdrawal', 'split', 'other')),
    raw_symbol           TEXT,
    ticker               TEXT,                          -- NULL = not covered / no symbol
    quantity             REAL,                          -- signed: sells negative
    price                REAL,
    amount               REAL,
    fee                  REAL,
    currency             TEXT,
    trade_date           TEXT,
    settle_date          TEXT,
    description          TEXT,
    synced_at            TEXT NOT NULL,
    UNIQUE (connection_id, provider_activity_id)
);

CREATE INDEX IF NOT EXISTS idx_broker_activities_portfolio
    ON broker_activities(portfolio_id, trade_date);
