-- Trader-ready UX (roadmap 13.2 and 13.4).

-- First-run guide: each step a person finished or skipped. A step that the
-- data already shows as done (a portfolio, a subscription, a push device)
-- needs no row; a row records a skip, or a done step with nothing to derive
-- it from. One row per user and step.
CREATE TABLE IF NOT EXISTS onboarding_steps (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    step       TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('done', 'skipped')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, step)
);

-- The guide as a whole: closed by the person, so it stops showing.
CREATE TABLE IF NOT EXISTS onboarding_status (
    user_id      TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    dismissed_at TEXT
);

-- Watchlists: a person's own ticker lists, a quick universe for the lab
-- and a filter on Today and the charts. Never shared.
CREATE TABLE IF NOT EXISTS watchlists (
    id           TEXT PRIMARY KEY,                -- wl_<hex>
    owner_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    tickers_json TEXT NOT NULL DEFAULT '[]',      -- ordered, unique instrument ids
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (owner_id, name)
);

CREATE INDEX IF NOT EXISTS idx_watchlists_owner ON watchlists(owner_id, created_at);
