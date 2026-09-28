-- Economic release alerts (roadmap 20.9): each person picks the countries
-- they hear about and the lowest importance that alerts them.
--
-- One row per person who made a choice. No row: the defaults apply, the
-- countries of the person's portfolios' base currencies (else US) and high
-- importance only. countries_json NULL also means the default countries,
-- so a person can change the threshold and keep following their
-- portfolios. The on/off switch stays in event_alert_prefs (topic
-- 'economic').
CREATE TABLE IF NOT EXISTS economic_alert_prefs (
    user_id        TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    countries_json TEXT,                  -- JSON list of country codes, or NULL
    min_importance TEXT NOT NULL DEFAULT 'high'
                   CHECK (min_importance IN ('low', 'medium', 'high')),
    updated_at     TEXT NOT NULL          -- ISO-8601 UTC
);
