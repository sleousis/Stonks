-- Transaction cost analysis and the trade journal (BL-32, roadmap 9.3.4;
-- principles P21, P22 and P42).
--
-- Every order the tick places records what the strategy decided and what it
-- expected to pay:
--
--   decision_price         the price the strategy decided at (the latest close)
--   decided_at             when it decided (ISO-8601 UTC, the tick's as_of)
--   decision_context_json  why: trigger, strategy, signal score and rank,
--                          constructor, target weight (JSON object)
--   expected_cost_bps      the cost model's estimate at the decision price
--
-- and, once the next session's bar is ingested (production.tca.refresh_benchmarks):
--
--   benchmark_price        the next session's open, the price a backtest
--                          fills at, so live and backtest conventions compare
--   post_close_price       that session's close, for the opportunity cost of
--                          what did not fill
--
-- fills.arrival_price is the market price when the order reached the market,
-- before costs: the close the simulated broker filled at, or for an external
-- broker the next session's open once it is known.
--
-- Rows written before this migration keep NULLs and are left out of TCA.

ALTER TABLE orders ADD COLUMN decision_price REAL;
ALTER TABLE orders ADD COLUMN decided_at TEXT;
ALTER TABLE orders ADD COLUMN decision_context_json TEXT;
ALTER TABLE orders ADD COLUMN expected_cost_bps REAL;
ALTER TABLE orders ADD COLUMN benchmark_price REAL;
ALTER TABLE orders ADD COLUMN post_close_price REAL;

ALTER TABLE fills ADD COLUMN arrival_price REAL;

CREATE INDEX IF NOT EXISTS idx_orders_decided ON orders(portfolio_id, decided_at);

-- Free-text notes a trader adds to an order in the journal. A note belongs
-- to the order's portfolio; only its author edits it.
CREATE TABLE IF NOT EXISTS journal_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_client_id TEXT NOT NULL REFERENCES orders(client_id),
    portfolio_id    TEXT NOT NULL REFERENCES portfolios(id),
    author          TEXT NOT NULL CHECK (length(trim(author)) > 0),  -- "user:<id>" | "service:<name>"
    note            TEXT NOT NULL CHECK (length(trim(note)) > 0),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_notes_order ON journal_notes(order_client_id, id);
CREATE INDEX IF NOT EXISTS idx_journal_notes_portfolio ON journal_notes(portfolio_id, id);

-- A note stays with its order's portfolio.
CREATE TRIGGER IF NOT EXISTS journal_notes_portfolio_check
BEFORE INSERT ON journal_notes
WHEN NEW.portfolio_id IS NOT (SELECT portfolio_id FROM orders WHERE client_id = NEW.order_client_id)
BEGIN
    SELECT RAISE(ABORT, 'journal_notes.portfolio_id must be its order''s portfolio');
END;
