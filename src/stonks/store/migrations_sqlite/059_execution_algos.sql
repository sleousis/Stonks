-- Roadmap 23.16: execution algorithms.
--
-- execution_algo_settings  how a portfolio's orders are worked. strategy_id ''
--                          is the portfolio's own setting, a strategy id
--                          overrides it for that strategy's orders. No row
--                          means plain orders (the default).
-- orders.exec_algo         the algo an order was worked with (NULL: plain).
-- orders.parent_client_id  the parent order of a child slice Stonks sent.
-- algo_parents             a parent order Stonks works as child slices, at a
--                          broker that does not run the algo itself. Its
--                          state follows its children (execution/algos/slicer.py).
-- algo_slices              the parent's planned child orders.

CREATE TABLE IF NOT EXISTS execution_algo_settings (
    portfolio_id TEXT NOT NULL,
    strategy_id  TEXT NOT NULL DEFAULT '',
    algo         TEXT NOT NULL,
    params_json  TEXT NOT NULL DEFAULT '{}',
    updated_by   TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, strategy_id)
);

ALTER TABLE orders ADD COLUMN exec_algo TEXT;
ALTER TABLE orders ADD COLUMN parent_client_id TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_parent
    ON orders(parent_client_id) WHERE parent_client_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS algo_parents (
    client_id    TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    ticket_id    TEXT,
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity     REAL NOT NULL CHECK (quantity > 0),
    algo         TEXT NOT NULL,
    order_json   TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'accepted',
    state_reason TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_algo_parents_state ON algo_parents(state, portfolio_id);

CREATE TABLE IF NOT EXISTS algo_slices (
    parent_client_id TEXT NOT NULL REFERENCES algo_parents(client_id),
    seq              INTEGER NOT NULL,
    child_client_id  TEXT NOT NULL UNIQUE,
    quantity         REAL NOT NULL CHECK (quantity > 0),
    send_after       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'planned'
                     CHECK (status IN ('planned', 'sent', 'skipped')),
    status_reason    TEXT,
    sent_at          TEXT,
    PRIMARY KEY (parent_client_id, seq)
);
