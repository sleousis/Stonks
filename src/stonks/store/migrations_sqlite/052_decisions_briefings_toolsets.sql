-- Why did or didn't we trade, briefings and MCP toolsets (roadmap 23.7, 23.8).
--
-- trade_decisions   per tick, book and ticker, the step that kept the
--                   ticker out or trimmed it (universe, rank, constructor,
--                   buffer, stale_price, risk_rule, scope, external, halt,
--                   held, traded). strategies holds every strategy of the
--                   book that scored the ticker as ",a,b," so a strategy
--                   page can match one. detail_json holds the rule, the
--                   quantities or the weights. Old rows are pruned.
-- briefing_prefs    per person, the research-only briefings before the open
--                   and after the close. Off when there is no row.
-- api_tokens.toolsets  JSON list of MCP tool groups the token may use.
--                   NULL means every group (tokens made before 23.8).

CREATE TABLE IF NOT EXISTS trade_decisions (
    tick_id      TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    step         TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('traded', 'trimmed', 'kept_out', 'held')),
    strategy_id  TEXT,
    strategies   TEXT NOT NULL DEFAULT '',
    score        REAL,
    detail_json  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tick_id, portfolio_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_trade_decisions_ticker
    ON trade_decisions(portfolio_id, ticker, as_of);
CREATE INDEX IF NOT EXISTS idx_trade_decisions_as_of
    ON trade_decisions(portfolio_id, as_of);

CREATE TABLE IF NOT EXISTS briefing_prefs (
    user_id     TEXT PRIMARY KEY REFERENCES users(id),
    pre_open    INTEGER NOT NULL DEFAULT 0 CHECK (pre_open IN (0, 1)),
    post_close  INTEGER NOT NULL DEFAULT 0 CHECK (post_close IN (0, 1)),
    updated_at  TEXT NOT NULL
);

ALTER TABLE api_tokens ADD COLUMN toolsets TEXT;
