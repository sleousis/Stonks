-- Saved screens (roadmap 20.8): a person's own screener filters. spec_json
-- is a ScreenSpec (stonks.screener.spec). Never shared, like watchlists.
CREATE TABLE IF NOT EXISTS screens (
    id         TEXT PRIMARY KEY,                -- scr_<hex>
    owner_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    spec_json  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner_id, name)
);

CREATE INDEX IF NOT EXISTS idx_screens_owner ON screens(owner_id, created_at);
