-- Strategy registry (Block 4)
CREATE TABLE IF NOT EXISTS strategies (
    id            TEXT PRIMARY KEY,
    class_path    TEXT NOT NULL,              -- e.g. "stonks.strategies.value_screen:ValueScreen"
    params_json   TEXT NOT NULL,              -- frozen Params dict
    artifact_path TEXT,                       -- data/artifacts/<id>/; NULL for rule-based
    status        TEXT NOT NULL CHECK (status IN ('active', 'shadow', 'retired')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS survival_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id  TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    test_id      TEXT NOT NULL,
    passed       INTEGER NOT NULL CHECK (passed IN (0, 1)),
    metrics_json TEXT NOT NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_strategy ON survival_reports(strategy_id);

-- Production tick ledger (Block 5)
CREATE TABLE IF NOT EXISTS tick_runs (
    id           TEXT PRIMARY KEY,            -- ulid
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'error')),
    summary_json TEXT
);

-- Orders + fills (Block 5)
CREATE TABLE IF NOT EXISTS orders (
    client_id       TEXT PRIMARY KEY,         -- idempotency key; safe to resubmit
    tick_id         TEXT REFERENCES tick_runs(id),
    strategy_id     TEXT REFERENCES strategies(id),
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        REAL NOT NULL,
    order_type      TEXT NOT NULL CHECK (order_type IN ('market', 'limit', 'stop', 'stop_limit')),
    limit_price     REAL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'filled', 'partially_filled', 'rejected', 'cancelled')),
    broker_order_id TEXT,                     -- vendor-side id after placement
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_tick     ON orders(tick_id);
CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id);

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_client_id TEXT NOT NULL REFERENCES orders(client_id),
    ticker          TEXT NOT NULL,
    quantity        REAL NOT NULL,
    price           REAL NOT NULL,
    fee             REAL NOT NULL DEFAULT 0,
    filled_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_client_id);

-- Portfolio snapshots (Block 5)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id        TEXT REFERENCES tick_runs(id),
    taken_at       TEXT NOT NULL,
    cash           REAL NOT NULL,
    positions_json TEXT NOT NULL,             -- JSON: {ticker: quantity}
    total_value    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_tick ON portfolio_snapshots(tick_id);
