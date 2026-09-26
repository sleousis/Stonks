-- Strategy Studio drafts (Phase 5.5): rule specs or (opt-in) Python code
-- being edited, tested and finally registered as a strategy.
-- spec_json holds the RuleSpec for kind='rule' and the constructor params
-- for kind='code'; source_code is only set for kind='code'.
CREATE TABLE IF NOT EXISTS strategy_drafts (
    id                     TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    kind                   TEXT NOT NULL CHECK (kind IN ('rule', 'code')),
    spec_json              TEXT NOT NULL DEFAULT '{}',
    source_code            TEXT,
    status                 TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'registered')),
    registered_strategy_id TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_drafts_updated ON strategy_drafts(updated_at);
