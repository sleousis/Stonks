-- Notifications (roadmap 15.6 backend, step S4 of docs/design/accounts-and-modes.md).
--
-- The router writes one notification_outbox row per (user, event) and one
-- notification_deliveries row per (notification, channel, target) in the
-- producer's transaction; a delivery worker sends them later, so a tick
-- never waits on a push service. The in-app feed is the alerts table
-- (user_id, category, dedupe_key, read_at since 010); every outbox row links
-- its alert.
--
-- Payloads are minimal on purpose (title, one line, deep link): no amounts or
-- holdings, because push services see metadata. Details load in the app.

-- ---- outbox ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category      TEXT NOT NULL CHECK (category IN ('signal', 'order', 'risk', 'system')),
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

CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_user_dedupe
    ON notification_outbox(user_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND dedupe_active = 1;
CREATE INDEX IF NOT EXISTS idx_outbox_user ON notification_outbox(user_id, id);

-- ---- deliveries -----------------------------------------------------------
-- status: pending (due at next_attempt_at), deferred (quiet hours), sending
-- (claimed by a worker until next_attempt_at, the lease), sent, failed
-- (transient error, retried at next_attempt_at), dead (gave up or the target
-- is gone), skipped (the target was removed before delivery).
CREATE TABLE IF NOT EXISTS notification_deliveries (
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

CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON notification_deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_user ON notification_deliveries(user_id, id);

-- ---- Web Push subscriptions (one row per browser or installed app) --------
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id              TEXT PRIMARY KEY,                -- psh_<hex>
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint        TEXT NOT NULL UNIQUE,            -- capability URL: never logged
    p256dh          TEXT NOT NULL,
    auth            TEXT NOT NULL,
    user_agent      TEXT,
    created_at      TEXT NOT NULL,
    last_success_at TEXT,
    failure_count   INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),  -- consecutive
    revoked_at      TEXT,
    revoked_reason  TEXT                             -- user, gone, failures, replaced
);

CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions(user_id);

-- ---- preferences ----------------------------------------------------------
-- Most specific row wins: (category, strategy) over (category, any strategy)
-- over the channel's default. strategy_id NULL = every strategy.
CREATE TABLE IF NOT EXISTS notification_prefs (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category    TEXT NOT NULL CHECK (category IN ('signal', 'order', 'risk', 'system')),
    strategy_id TEXT,
    channel     TEXT NOT NULL,
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_prefs
    ON notification_prefs(user_id, category, COALESCE(strategy_id, ''), channel);

-- ---- per-user settings ----------------------------------------------------
-- Quiet hours are wall-clock times in users.timezone; start = end (or NULL)
-- means none. webhook_url is the user's own optional webhook (a secret:
-- never logged or returned in full).
CREATE TABLE IF NOT EXISTS notification_settings (
    user_id     TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    quiet_start TEXT CHECK (quiet_start IS NULL OR
                            (quiet_start GLOB '[0-2][0-9]:[0-5][0-9]' AND quiet_start < '24:00')),
    quiet_end   TEXT CHECK (quiet_end IS NULL OR
                            (quiet_end GLOB '[0-2][0-9]:[0-5][0-9]' AND quiet_end < '24:00')),
    webhook_url TEXT,
    updated_at  TEXT NOT NULL,
    CHECK ((quiet_start IS NULL) = (quiet_end IS NULL))
);
