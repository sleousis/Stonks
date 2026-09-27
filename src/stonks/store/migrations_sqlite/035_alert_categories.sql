-- Price alerts (20.2) and upcoming-event alerts (20.7) get their own
-- notification categories, price_alert and event_alert, so a person can
-- turn them off without losing strategy signals. event_alert_prefs holds
-- the per-kind switches (earnings, dividends, economic), on by default.
--
-- SQLite cannot change a CHECK, so alerts, notification_outbox,
-- notification_deliveries and notification_prefs are rebuilt with their
-- rows, ids and indexes. None of them has triggers. The order matters:
--   * notification_outbox references alerts(id) and dropping a referenced
--     table fails, so the outbox goes before alerts is rebuilt.
--   * dropping notification_outbox would cascade into
--     notification_deliveries, so deliveries go first and come back after.
--   * AUTOINCREMENT counters live in sqlite_sequence and a DROP forgets
--     them, so they are kept aside and written back: an id is never reused.

CREATE TEMP TABLE _seq_034 AS
    SELECT name, seq FROM sqlite_sequence
     WHERE name IN ('alerts', 'notification_outbox', 'notification_deliveries');
CREATE TEMP TABLE _outbox_034 AS SELECT * FROM notification_outbox;
CREATE TEMP TABLE _deliveries_034 AS SELECT * FROM notification_deliveries;

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
CREATE TABLE alerts_034 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    level        TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    title        TEXT NOT NULL,
    message      TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',   -- JSON object: Notification.fields
    created_at   TEXT NOT NULL,                -- ISO-8601 UTC
    user_id      TEXT REFERENCES users(id),    -- NULL = audience "admins"
    category     TEXT CHECK (category IS NULL OR category IN
                    ('signal', 'order', 'risk', 'system', 'price_alert', 'event_alert')),
    dedupe_key   TEXT,
    read_at      TEXT
);

INSERT INTO alerts_034 (id, level, title, message, context_json, created_at, user_id,
    category, dedupe_key, read_at)
SELECT id, level, title, message, context_json, created_at, user_id, category, dedupe_key,
    read_at
FROM alerts;

DROP TABLE alerts;
ALTER TABLE alerts_034 RENAME TO alerts;

CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts(level, id);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, id);

-- ---- outbox ---------------------------------------------------------------
CREATE TABLE notification_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category      TEXT NOT NULL CHECK (category IN
                     ('signal', 'order', 'risk', 'system', 'price_alert', 'event_alert')),
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
FROM _outbox_034;

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
FROM _deliveries_034;

CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON notification_deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_user ON notification_deliveries(user_id, id);

-- ---- preferences ----------------------------------------------------------
CREATE TABLE notification_prefs_034 (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category    TEXT NOT NULL CHECK (category IN
                   ('signal', 'order', 'risk', 'system', 'price_alert', 'event_alert')),
    strategy_id TEXT,
    channel     TEXT NOT NULL,
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at  TEXT NOT NULL
);

INSERT INTO notification_prefs_034 (user_id, category, strategy_id, channel, enabled, updated_at)
SELECT user_id, category, strategy_id, channel, enabled, updated_at FROM notification_prefs;

DROP TABLE notification_prefs;
ALTER TABLE notification_prefs_034 RENAME TO notification_prefs;

CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_prefs
    ON notification_prefs(user_id, category, COALESCE(strategy_id, ''), channel);

-- ---- AUTOINCREMENT counters -----------------------------------------------
DELETE FROM sqlite_sequence
 WHERE name IN ('alerts', 'notification_outbox', 'notification_deliveries');
INSERT INTO sqlite_sequence (name, seq) SELECT name, seq FROM _seq_034;

DROP TABLE _seq_034;
DROP TABLE _outbox_034;
DROP TABLE _deliveries_034;

-- ---- per-kind event alert switches -----------------------------------------
-- One row per choice a person made. No row: the kind is on. topic is an
-- open set checked in Python (stonks.notify.prefs.EVENT_ALERT_TOPICS), so
-- a new kind needs no table rebuild.
CREATE TABLE IF NOT EXISTS event_alert_prefs (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    topic      TEXT NOT NULL,
    enabled    INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, topic)
);
