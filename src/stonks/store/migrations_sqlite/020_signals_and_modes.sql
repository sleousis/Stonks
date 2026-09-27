-- Signal phase and automation modes (roadmap 15.5, steps S5 and S6 of
-- docs/design/accounts-and-modes.md).
--
-- signals and signal_events are global: a strategy is scored once per tick
-- for everyone. portfolio_runs is scoped: one row per portfolio per tick,
-- the source of the paper-day count of the auto gate (decision 2026-09-26).

-- ---- signals: every score of one tick, per strategy and ticker -------------
-- A re-run of the same as_of keeps the first rows (the signal phase is
-- keyed by (strategy_id, as_of), like the model books).
CREATE TABLE IF NOT EXISTS signals (
    as_of        TEXT NOT NULL,                  -- ISO date
    strategy_id  TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    tick_id      TEXT NOT NULL,
    score        REAL,                           -- expected return; NULL: held by the model book, not scored
    rank         INTEGER,                        -- 1 = best score of the strategy that day
    model_weight REAL,                           -- weight in the model book after the tick; NULL: no model book
    PRIMARY KEY (as_of, strategy_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_signals_tick ON signals(tick_id);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id, as_of);

-- ---- signal_events: what changed, with a plain reason ----------------------
CREATE TABLE IF NOT EXISTS signal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of       TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('entry', 'exit', 'increase', 'decrease', 'risk')),
    tick_id     TEXT NOT NULL,
    strength    REAL,                            -- the score, or the model weight change
    reason_json TEXT NOT NULL DEFAULT '{}',      -- the strategy's explain() or the default reason
    created_at  TEXT NOT NULL,
    UNIQUE (as_of, strategy_id, ticker, kind)
);

CREATE INDEX IF NOT EXISTS idx_signal_events_as_of ON signal_events(as_of, strategy_id);

-- ---- portfolio_runs: one row per portfolio per tick ------------------------
-- paper_subscriptions_json lists the paper subscriptions the run traded;
-- a subscription's paper days are the distinct as_of of its runs that
-- finished without an error or a risk breach, after its last breach and
-- after subscriptions.paper_since.
CREATE TABLE IF NOT EXISTS portfolio_runs (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id                  TEXT NOT NULL,
    portfolio_id             TEXT NOT NULL,
    as_of                    TEXT NOT NULL,
    mode                     TEXT NOT NULL CHECK (mode IN ('legacy', 'paper', 'auto')),
    status                   TEXT NOT NULL
                             CHECK (status IN ('ok', 'partial', 'error', 'noop', 'paused')),
    orders_placed            INTEGER NOT NULL DEFAULT 0,
    fills                    INTEGER NOT NULL DEFAULT 0,
    orders_rejected          INTEGER NOT NULL DEFAULT 0,
    risk_breached            INTEGER NOT NULL DEFAULT 0 CHECK (risk_breached IN (0, 1)),
    halted                   TEXT CHECK (halted IS NULL OR halted IN ('buys', 'all')),
    error                    TEXT,
    paper_subscriptions_json TEXT NOT NULL DEFAULT '[]',
    auto_subscriptions_json  TEXT NOT NULL DEFAULT '[]',
    started_at               TEXT NOT NULL,
    finished_at              TEXT NOT NULL,
    UNIQUE (tick_id, portfolio_id)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_runs_portfolio ON portfolio_runs(portfolio_id, as_of);

-- ---- paper accounts of broker portfolios -----------------------------------
-- Paper subscriptions of a broker portfolio trade a simulated account of
-- their own: a portfolio row with paper_of = the broker portfolio, created
-- by the tick, owned by the same user, hidden from portfolio lists.
ALTER TABLE portfolios ADD COLUMN paper_of TEXT REFERENCES portfolios(id);

-- ---- paper-day count ------------------------------------------------------
-- Set when a subscription switches to notify: runs before it don't count.
-- (paper_days_completed and paper_last_as_of from 010 are no longer
-- written; the count comes from portfolio_runs.)
ALTER TABLE subscriptions ADD COLUMN paper_since TEXT;

-- ---- per-user risk limits --------------------------------------------------
-- A partial RiskPolicy that tightens every portfolio of the user.
ALTER TABLE users ADD COLUMN risk_policy_json TEXT NOT NULL DEFAULT '{}';
