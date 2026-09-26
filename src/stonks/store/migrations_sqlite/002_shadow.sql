-- Shadow mode (roadmap 2.4): hypothetical orders of shadow strategies and
-- their per-strategy virtual portfolios. Never touched by the real ledger
-- (orders / fills / portfolio_snapshots).

CREATE TABLE IF NOT EXISTS shadow_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id     TEXT NOT NULL REFERENCES tick_runs(id),
    strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    as_of       TEXT NOT NULL,                -- ISO date the decision was made for
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    REAL NOT NULL,                -- filled quantity, or proposed if rejected
    price       REAL,                         -- simulated fill price; quoted price if rejected
    status      TEXT NOT NULL CHECK (status IN ('filled', 'rejected')),
    created_at  TEXT NOT NULL,
    UNIQUE (strategy_id, as_of, ticker, side)
);

CREATE INDEX IF NOT EXISTS idx_shadow_decisions_strategy ON shadow_decisions(strategy_id, as_of);

CREATE TABLE IF NOT EXISTS shadow_portfolio_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id        TEXT NOT NULL REFERENCES tick_runs(id),
    strategy_id    TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    as_of          TEXT NOT NULL,             -- one virtual snapshot per strategy per day
    taken_at       TEXT NOT NULL,
    cash           REAL NOT NULL,
    positions_json TEXT NOT NULL,             -- JSON: {ticker: quantity}
    total_value    REAL NOT NULL,
    UNIQUE (strategy_id, as_of)
);
