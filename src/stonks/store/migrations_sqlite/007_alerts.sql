-- Every notification sent through the Notifier seam (StoreNotifier), for
-- the trader console's alerts feed (GET /api/alerts). Title, message and
-- context are redacted before they are written.
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    level        TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    title        TEXT NOT NULL,
    message      TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',   -- JSON object: Notification.fields
    created_at   TEXT NOT NULL                 -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts(level, id);
