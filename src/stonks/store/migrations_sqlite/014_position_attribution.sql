-- Position attribution (BL-12, W2.1 of docs/research/book-lessons.md).
--
-- After a portfolio trades, the tick records which strategy (and which
-- subscription) each held position belongs to, as shares summing to 1 per
-- (portfolio_id, as_of, ticker). P&L per strategy per portfolio is the
-- position's P&L times the share in force that day (the latest row on or
-- before it). An order's split is the rows of its (portfolio_id, as_of,
-- ticker); orders.strategy_id names the largest share.
--
-- source: 'target'   a constructor's target book (share of the weight)
--         'decision' single_winner: the strategy whose decide bought it
--         'carried'  unchanged holding, shares copied from its last row
-- A same-day re-run replaces that day's rows of the portfolio.

CREATE TABLE IF NOT EXISTS position_attribution (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id         TEXT NOT NULL REFERENCES tick_runs(id),
    portfolio_id    TEXT NOT NULL REFERENCES portfolios(id),
    as_of           TEXT NOT NULL,                    -- ISO date of the tick
    ticker          TEXT NOT NULL,
    strategy_id     TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    subscription_id TEXT REFERENCES subscriptions(id) ON DELETE SET NULL,
    quantity        REAL NOT NULL,                    -- position after the tick
    target_weight   REAL,                             -- the ticker's target; NULL unless 'target'
    weight_share    REAL NOT NULL,                    -- this strategy's share
    source          TEXT NOT NULL CHECK (source IN ('target', 'decision', 'carried')),
    created_at      TEXT NOT NULL,
    UNIQUE (portfolio_id, as_of, ticker, strategy_id)
);

CREATE INDEX IF NOT EXISTS idx_position_attribution_strategy
    ON position_attribution(strategy_id, portfolio_id, as_of);
