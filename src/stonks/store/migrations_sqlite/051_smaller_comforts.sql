-- Roadmap 23.17, smaller comforts.
--
-- 1. Screen alerts get their own notification category, screen_alert, so a
--    person can turn them off without losing price alerts or signals. The
--    CHECK lists are rebuilt the way 035 rebuilt them: alerts,
--    notification_outbox, notification_deliveries and notification_prefs
--    keep their rows, ids, indexes and AUTOINCREMENT counters.
-- 2. screen_alerts, screen_alert_matches and screen_alert_events: a saved
--    screen that runs on a schedule and notifies about names that newly
--    match.
-- 3. demo_portfolios: the sample portfolio a new person can open. Only the
--    seed is stored. Its figures are computed from synthetic bars and never
--    touch portfolios, orders, fills or the lake.
-- 4. statement_imports and broker_activities.import_id: CSV statements from
--    brokers without an API, written into the tables a broker sync fills,
--    with an undo per import.

CREATE TEMP TABLE _seq_051 AS
    SELECT name, seq FROM sqlite_sequence
     WHERE name IN ('alerts', 'notification_outbox', 'notification_deliveries');
CREATE TEMP TABLE _outbox_051 AS SELECT * FROM notification_outbox;
CREATE TEMP TABLE _deliveries_051 AS SELECT * FROM notification_deliveries;

DROP INDEX IF EXISTS idx_deliveries_due;
DROP INDEX IF EXISTS idx_deliveries_user;
DROP INDEX IF EXISTS ux_outbox_user_dedupe;
DROP INDEX IF EXISTS idx_outbox_user;
DROP INDEX IF EXISTS idx_alerts_level;
DROP INDEX IF EXISTS idx_alerts_user;
DROP INDEX IF EXISTS ux_notification_prefs;

DROP TABLE notification_deliveries;
DROP TABLE notification_outbox;

-- ---- alerts (the in-app feed) ---------------------------------------------
CREATE TABLE alerts_051 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    level        TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    title        TEXT NOT NULL,
    message      TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',   -- JSON object: Notification.fields
    created_at   TEXT NOT NULL,                -- ISO-8601 UTC
    user_id      TEXT REFERENCES users(id),    -- NULL = audience "admins"
    category     TEXT CHECK (category IS NULL OR category IN
                    ('signal', 'order', 'risk', 'system', 'price_alert', 'event_alert',
                     'screen_alert')),
    dedupe_key   TEXT,
    read_at      TEXT
);

INSERT INTO alerts_051 (id, level, title, message, context_json, created_at, user_id,
    category, dedupe_key, read_at)
SELECT id, level, title, message, context_json, created_at, user_id, category, dedupe_key,
    read_at
FROM alerts;

DROP TABLE alerts;
ALTER TABLE alerts_051 RENAME TO alerts;

CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts(level, id);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, id);

-- ---- outbox ---------------------------------------------------------------
CREATE TABLE notification_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category      TEXT NOT NULL CHECK (category IN
                     ('signal', 'order', 'risk', 'system', 'price_alert', 'event_alert',
                     'screen_alert')),
    level         TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    urgency       TEXT NOT NULL CHECK (urgency IN ('low', 'normal', 'high')),
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    deep_link     TEXT,                              -- app-relative path, e.g. /signals
    strategy_id   TEXT,
    portfolio_id  TEXT,
    dedupe_key    TEXT,                              -- NULL = never deduplicated
    -- 1 while the key blocks repeats; the router sets 0 once the dedupe
    -- window has passed, so the same key can fire again.
    dedupe_active INTEGER NOT NULL DEFAULT 1 CHECK (dedupe_active IN (0, 1)),
    alert_id      INTEGER REFERENCES alerts(id),      -- the in-app feed row
    created_at    TEXT NOT NULL                      -- ISO-8601 UTC
);

INSERT INTO notification_outbox (id, user_id, category, level, urgency, title, body, deep_link,
    strategy_id, portfolio_id, dedupe_key, dedupe_active, alert_id, created_at)
SELECT id, user_id, category, level, urgency, title, body, deep_link, strategy_id,
    portfolio_id, dedupe_key, dedupe_active, alert_id, created_at
FROM _outbox_051;

CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_user_dedupe
    ON notification_outbox(user_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND dedupe_active = 1;
CREATE INDEX IF NOT EXISTS idx_outbox_user ON notification_outbox(user_id, id);

-- ---- deliveries (unchanged, rebuilt only because the outbox was) ----------
CREATE TABLE notification_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notification_outbox(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    target_id       TEXT NOT NULL DEFAULT '',        -- push_subscriptions.id, or '' for one-target channels
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'deferred', 'sending', 'sent', 'failed',
                                      'dead', 'skipped')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    deferred        INTEGER NOT NULL DEFAULT 0 CHECK (deferred IN (0, 1)),  -- held by quiet hours
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,                            -- redacted
    sent_at         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (notification_id, channel, target_id)
);

INSERT INTO notification_deliveries (id, notification_id, user_id, channel, target_id, status,
    attempts, deferred, next_attempt_at, last_error, sent_at, created_at, updated_at)
SELECT id, notification_id, user_id, channel, target_id, status, attempts, deferred,
    next_attempt_at, last_error, sent_at, created_at, updated_at
FROM _deliveries_051;

CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON notification_deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_user ON notification_deliveries(user_id, id);

-- ---- preferences ----------------------------------------------------------
CREATE TABLE notification_prefs_051 (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category    TEXT NOT NULL CHECK (category IN
                   ('signal', 'order', 'risk', 'system', 'price_alert', 'event_alert',
                     'screen_alert')),
    strategy_id TEXT,
    channel     TEXT NOT NULL,
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at  TEXT NOT NULL
);

INSERT INTO notification_prefs_051 (user_id, category, strategy_id, channel, enabled, updated_at)
SELECT user_id, category, strategy_id, channel, enabled, updated_at FROM notification_prefs;

DROP TABLE notification_prefs;
ALTER TABLE notification_prefs_051 RENAME TO notification_prefs;

CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_prefs
    ON notification_prefs(user_id, category, COALESCE(strategy_id, ''), channel);

-- ---- AUTOINCREMENT counters -----------------------------------------------
DELETE FROM sqlite_sequence
 WHERE name IN ('alerts', 'notification_outbox', 'notification_deliveries');
INSERT INTO sqlite_sequence (name, seq) SELECT name, seq FROM _seq_051;

DROP TABLE _seq_051;
DROP TABLE _outbox_051;
DROP TABLE _deliveries_051;

-- ---- screen alerts ------------------------------------------------------------
-- One row per saved screen that alerts. cadence daily runs on every run of
-- the screen_alerts job, weekly only on weekday (0 = Monday).
CREATE TABLE IF NOT EXISTS screen_alerts (
    screen_id   TEXT PRIMARY KEY REFERENCES screens(id) ON DELETE CASCADE,
    owner_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    cadence     TEXT NOT NULL DEFAULT 'daily' CHECK (cadence IN ('daily', 'weekly')),
    weekday     INTEGER CHECK (weekday IS NULL OR weekday BETWEEN 0 AND 6),
    last_as_of  TEXT,                                -- the last day it ran on
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    CHECK (cadence = 'daily' OR weekday IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_screen_alerts_owner ON screen_alerts(owner_id);

-- The names the screen matched on its last run. The next run alerts about
-- the names not in here. The first run only fills it (a baseline).
CREATE TABLE IF NOT EXISTS screen_alert_matches (
    screen_id  TEXT NOT NULL REFERENCES screen_alerts(screen_id) ON DELETE CASCADE,
    ticker     TEXT NOT NULL,
    since      TEXT NOT NULL,                        -- the day it first matched
    PRIMARY KEY (screen_id, ticker)
);

-- Each run that found new names. tickers_json is a JSON list.
CREATE TABLE IF NOT EXISTS screen_alert_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id     TEXT NOT NULL REFERENCES screens(id) ON DELETE CASCADE,
    owner_id      TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    tickers_json  TEXT NOT NULL,
    matched       INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (screen_id, as_of)
);

CREATE INDEX IF NOT EXISTS idx_screen_alert_events_owner ON screen_alert_events(owner_id, id);

-- ---- demo portfolio -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS demo_portfolios (
    user_id    TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    seed       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- ---- CSV statement imports ----------------------------------------------------------
-- One row per committed CSV file. The rows it added carry its id in
-- broker_activities.import_id, so an undo removes exactly those.
CREATE TABLE IF NOT EXISTS statement_imports (
    id             TEXT PRIMARY KEY,                 -- imp_<hex>
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    portfolio_id   TEXT NOT NULL REFERENCES portfolios(id),
    connection_id  TEXT NOT NULL REFERENCES broker_connections(id) ON DELETE CASCADE,
    filename       TEXT,
    mapping_json   TEXT NOT NULL,
    rows_total     INTEGER NOT NULL,
    rows_added     INTEGER NOT NULL,
    rows_duplicate INTEGER NOT NULL,
    rows_skipped   INTEGER NOT NULL,
    first_date     TEXT,
    last_date      TEXT,
    created_at     TEXT NOT NULL,
    undone_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_statement_imports_user ON statement_imports(user_id, created_at);

ALTER TABLE broker_activities ADD COLUMN import_id TEXT;

CREATE INDEX IF NOT EXISTS idx_broker_activities_import
    ON broker_activities(import_id) WHERE import_id IS NOT NULL;
