-- The round-trip journal (roadmap 23.3).
--
-- Round trips are built from fills on every read and never stored. A trade
-- is one opening fill (a lot): its id is that fill's id, so a trade keeps
-- its tags while it is open, after a partial exit and once it closes.
--
-- journal_playbooks   a person's named setups ("breakout", "earnings gap"),
--                     with the rules they mean to follow.
-- trade_annotations   per trade: the playbook, whether the plan was
--                     followed (NULL when not said), and a short review.
-- trade_labels        per trade: free tags and named mistakes.

CREATE TABLE IF NOT EXISTS journal_playbooks (
    id          TEXT PRIMARY KEY,                  -- pbk_<hex>
    owner_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
    description TEXT,
    archived    INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS trade_annotations (
    portfolio_id  TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    trade_id      INTEGER NOT NULL REFERENCES fills(id),   -- the opening fill
    playbook_id   TEXT REFERENCES journal_playbooks(id) ON DELETE SET NULL,
    followed_plan INTEGER CHECK (followed_plan IS NULL OR followed_plan IN (0, 1)),
    review        TEXT,
    updated_by    TEXT NOT NULL,                   -- "user:<id>" | "service:<name>"
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, trade_id)
);

CREATE TABLE IF NOT EXISTS trade_labels (
    portfolio_id TEXT NOT NULL,
    trade_id     INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('tag', 'mistake')),
    label        TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 40),
    PRIMARY KEY (portfolio_id, trade_id, kind, label),
    FOREIGN KEY (portfolio_id, trade_id)
        REFERENCES trade_annotations(portfolio_id, trade_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_labels_label ON trade_labels(portfolio_id, kind, label);

-- An annotation stays with its fill's portfolio.
CREATE TRIGGER IF NOT EXISTS trade_annotations_portfolio_check
BEFORE INSERT ON trade_annotations
WHEN NEW.portfolio_id IS NOT (
    SELECT COALESCE(f.portfolio_id, o.portfolio_id)
      FROM fills f LEFT JOIN orders o ON o.client_id = f.order_client_id
     WHERE f.id = NEW.trade_id
)
BEGIN
    SELECT RAISE(ABORT, 'trade_annotations.portfolio_id must be its fill''s portfolio');
END;
