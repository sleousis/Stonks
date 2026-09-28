-- System settings an admin edits in the console (complexity audit F61):
-- one row per overridden key, applied on top of the TOML config
-- (stonks.config_overrides). Only keys in the editable catalog are written,
-- never a secret. Every change also writes an audit_log row.
CREATE TABLE IF NOT EXISTS settings_overrides (
    key        TEXT PRIMARY KEY,                -- dotted path, e.g. production.risk.max_weight_per_ticker
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    reason     TEXT NOT NULL CHECK (length(trim(reason)) > 0)
);
