-- Roadmap 19.16: a short sale of a hard to borrow name waits for a person,
-- even in an auto book. order_tickets.hold takes hard_to_borrow next to
-- approve_mode and runaway.
--
-- SQLite cannot change a CHECK, so order_tickets is rebuilt with its rows,
-- indexes and the append-only trigger. Nothing references it, and a DROP
-- TABLE does not fire the delete trigger.

CREATE TABLE order_tickets_036 (
    id              TEXT PRIMARY KEY,                     -- tkt_<hex>
    portfolio_id    TEXT NOT NULL REFERENCES portfolios(id),
    tick_id         TEXT,
    as_of           TEXT NOT NULL,                        -- the decision day
    client_id       TEXT NOT NULL UNIQUE,                 -- the order it becomes
    strategy_id     TEXT,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    limit_price     REAL CHECK (limit_price IS NULL OR limit_price > 0),
    order_json      TEXT NOT NULL,                        -- the full order as decided
    preview_json    TEXT NOT NULL DEFAULT '{}',           -- reference price, notional, what-if
    reason_json     TEXT NOT NULL DEFAULT '{}',           -- signal, score, weight, rules
    hold            TEXT CHECK (hold IS NULL
                                OR hold IN ('approve_mode', 'runaway', 'hard_to_borrow')),
    status          TEXT NOT NULL
                    CHECK (status IN ('awaiting_approval', 'approved', 'rejected', 'expired',
                                      'submitted', 'filled', 'unfilled', 'cancelled',
                                      'failed')),
    submit_after    TEXT NOT NULL,                        -- the submit window opens
    expires_at      TEXT NOT NULL,                        -- the submit deadline
    decided_by      TEXT,                                 -- actor
    decided_at      TEXT,
    decision_reason TEXT,
    submitted_at    TEXT,
    status_reason   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (status <> 'rejected' OR length(trim(COALESCE(decision_reason, ''))) > 0)
);

INSERT INTO order_tickets_036 (id, portfolio_id, tick_id, as_of, client_id, strategy_id, ticker,
    side, quantity, limit_price, order_json, preview_json, reason_json, hold, status,
    submit_after, expires_at, decided_by, decided_at, decision_reason, submitted_at,
    status_reason, created_at, updated_at)
SELECT id, portfolio_id, tick_id, as_of, client_id, strategy_id, ticker,
    side, quantity, limit_price, order_json, preview_json, reason_json, hold, status,
    submit_after, expires_at, decided_by, decided_at, decision_reason, submitted_at,
    status_reason, created_at, updated_at
  FROM order_tickets;

DROP TRIGGER IF EXISTS order_tickets_no_delete;
DROP INDEX IF EXISTS idx_order_tickets_portfolio;
DROP INDEX IF EXISTS idx_order_tickets_due;
DROP INDEX IF EXISTS idx_order_tickets_tick;
DROP TABLE order_tickets;
ALTER TABLE order_tickets_036 RENAME TO order_tickets;

CREATE INDEX IF NOT EXISTS idx_order_tickets_portfolio ON order_tickets(portfolio_id, status);
CREATE INDEX IF NOT EXISTS idx_order_tickets_due ON order_tickets(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_order_tickets_tick ON order_tickets(tick_id);

CREATE TRIGGER IF NOT EXISTS order_tickets_no_delete
BEFORE DELETE ON order_tickets
BEGIN
    SELECT RAISE(ABORT, 'order_tickets is append-only');
END;
