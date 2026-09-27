-- Phase 20 (complete product): manual orders, price alerts, the Telegram
-- bot, the AI assistant, and tax settings per portfolio.

-- ---- 20.1 manual orders ------------------------------------------------------
-- origin: who decided the order. 'strategy' (every row so far, the tick) or
-- 'manual' (a person, from the console, MCP, the CLI or the assistant).
-- A manual order has no strategy_id. It records who placed it and why, and
-- the order it replaced when it was changed (cancel and replace).
ALTER TABLE orders ADD COLUMN origin TEXT NOT NULL DEFAULT 'strategy'
    CHECK (origin IN ('strategy', 'manual'));
ALTER TABLE orders ADD COLUMN manual_reason TEXT;
ALTER TABLE orders ADD COLUMN placed_by TEXT;
ALTER TABLE orders ADD COLUMN replaces_client_id TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_origin ON orders(portfolio_id, origin);

-- ---- 20.2 price alerts -------------------------------------------------------
-- A person's own alert rules on one ticker or on every ticker of one of
-- their watchlists. crosses_above and crosses_below fire when the price
-- moves through level. moves_pct fires when the price moved by at least pct
-- percent (either way) over the last window_days.
CREATE TABLE IF NOT EXISTS price_alert_rules (
    id             TEXT PRIMARY KEY,                -- pal_<hex>
    owner_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT,
    target_kind    TEXT NOT NULL CHECK (target_kind IN ('ticker', 'watchlist')),
    ticker         TEXT,
    watchlist_id   TEXT REFERENCES watchlists(id) ON DELETE CASCADE,
    condition      TEXT NOT NULL
                   CHECK (condition IN ('crosses_above', 'crosses_below', 'moves_pct')),
    level          REAL,
    pct            REAL CHECK (pct IS NULL OR pct > 0),
    window_days    INTEGER CHECK (window_days IS NULL OR window_days > 0),
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK ((target_kind = 'ticker' AND ticker IS NOT NULL AND watchlist_id IS NULL)
        OR (target_kind = 'watchlist' AND watchlist_id IS NOT NULL AND ticker IS NULL)),
    CHECK ((condition IN ('crosses_above', 'crosses_below') AND level IS NOT NULL)
        OR (condition = 'moves_pct' AND pct IS NOT NULL AND window_days IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_price_alert_rules_owner ON price_alert_rules(owner_id);

-- The last price each rule saw per ticker, so a crossing is found between
-- two checks whatever their spacing (a daily bar now, a live quote later).
CREATE TABLE IF NOT EXISTS price_alert_state (
    rule_id          TEXT NOT NULL REFERENCES price_alert_rules(id) ON DELETE CASCADE,
    ticker           TEXT NOT NULL,
    last_price       REAL NOT NULL,
    last_observed_at TEXT NOT NULL,                 -- ISO date or timestamp
    PRIMARY KEY (rule_id, ticker)
);

-- Each time a rule fired. One row per rule, ticker and observation.
CREATE TABLE IF NOT EXISTS price_alert_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         TEXT NOT NULL REFERENCES price_alert_rules(id) ON DELETE CASCADE,
    owner_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    price           REAL NOT NULL,
    detail          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (rule_id, ticker, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_price_alert_events_owner ON price_alert_events(owner_id, id);

-- ---- 20.3 Telegram -----------------------------------------------------------
-- A chat is linked to exactly one user, and a user to at most one chat.
-- Unlinking deletes the row.
CREATE TABLE IF NOT EXISTS telegram_links (
    chat_id    TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    username   TEXT,
    linked_at  TEXT NOT NULL
);

-- One-time link codes, stored as SHA-256. A code works once, before it expires.
CREATE TABLE IF NOT EXISTS telegram_link_codes (
    code_hash  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

-- Small bot state that must survive a restart (the long-poll offset).
CREATE TABLE IF NOT EXISTS telegram_bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---- 20.4 AI assistant -------------------------------------------------------
CREATE TABLE IF NOT EXISTS assistant_conversations (
    id         TEXT PRIMARY KEY,                    -- cnv_<hex>
    owner_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL DEFAULT '',
    -- 1: no write tool is offered in this conversation.
    research_only INTEGER NOT NULL DEFAULT 0 CHECK (research_only IN (0, 1)),
    -- Tool categories the assistant turned on beyond the default set.
    tool_categories_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assistant_conversations_owner
    ON assistant_conversations(owner_id, updated_at);

CREATE TABLE IF NOT EXISTS assistant_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content         TEXT NOT NULL DEFAULT '',
    tool_calls_json TEXT,                           -- the model's tool calls (assistant rows)
    tool_call_id    TEXT,                           -- the call a tool row answers
    tool_name       TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assistant_messages_conversation
    ON assistant_messages(conversation_id, id);

-- A write tool call waiting for the person to confirm or reject it.
CREATE TABLE IF NOT EXISTS assistant_pending_actions (
    id              TEXT PRIMARY KEY,               -- act_<hex>
    conversation_id TEXT NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    tool_call_id    TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    arguments_json  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'done', 'failed')),
    result_json     TEXT,
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

-- ---- 20.5 tax settings per portfolio -----------------------------------------
-- The base currency stays on portfolios.base_currency. lot_method picks
-- which lots a sale closes: fifo, or specific (tax_lot_picks, then FIFO for
-- the rest). wash_sales turns the US wash sale adjustment on.
CREATE TABLE IF NOT EXISTS portfolio_tax_settings (
    portfolio_id TEXT PRIMARY KEY REFERENCES portfolios(id) ON DELETE CASCADE,
    jurisdiction TEXT NOT NULL DEFAULT 'us' CHECK (jurisdiction IN ('us', 'eu', 'uk')),
    lot_method   TEXT NOT NULL DEFAULT 'fifo' CHECK (lot_method IN ('fifo', 'specific')),
    wash_sales   INTEGER NOT NULL DEFAULT 1 CHECK (wash_sales IN (0, 1)),
    updated_at   TEXT NOT NULL,
    updated_by   TEXT NOT NULL
);

-- Specific-lot picks: which buy fills a sell fill closes, and how much of each.
CREATE TABLE IF NOT EXISTS tax_lot_picks (
    portfolio_id TEXT NOT NULL,
    sell_fill_id INTEGER NOT NULL REFERENCES fills(id) ON DELETE CASCADE,
    buy_fill_id  INTEGER NOT NULL REFERENCES fills(id) ON DELETE CASCADE,
    quantity     REAL NOT NULL CHECK (quantity > 0),
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    PRIMARY KEY (sell_fill_id, buy_fill_id)
);

-- ---- 20.4 assistant safety ---------------------------------------------------
-- Order drafts: what the assistant may create instead of an order. A person
-- approves one in the web app with a fresh second factor, and only then it
-- is placed as a manual order through every check. The server computes the
-- reference price and notional. retry_key makes a repeated create a no-op.
CREATE TABLE IF NOT EXISTS order_drafts (
    id              TEXT PRIMARY KEY,               -- od_<hex>
    owner_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    portfolio_id    TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    source          TEXT NOT NULL CHECK (source IN ('assistant', 'console', 'mcp')),
    conversation_id TEXT,
    retry_key       TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    order_type      TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
    limit_price     REAL,
    reason          TEXT NOT NULL,
    reference_price REAL NOT NULL,
    notional        REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'placed', 'rejected', 'expired', 'cancelled')),
    client_id       TEXT,                           -- the manual order it became
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,
    decision_note   TEXT,
    UNIQUE (owner_id, retry_key)
);

CREATE INDEX IF NOT EXISTS idx_order_drafts_owner ON order_drafts(owner_id, status, created_at);

-- Every assistant turn: the model, the prompt version, the tool calls and
-- results, and the drafts it made.
CREATE TABLE IF NOT EXISTS assistant_turns (
    id              TEXT PRIMARY KEY,               -- trn_<hex>
    conversation_id TEXT NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    owner_id        TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    status          TEXT NOT NULL,
    steps           INTEGER NOT NULL DEFAULT 0,
    trace_json      TEXT NOT NULL DEFAULT '[]',
    draft_ids_json  TEXT NOT NULL DEFAULT '[]',
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_assistant_turns_conversation
    ON assistant_turns(conversation_id, started_at);

-- A burst of assistant writes freezes the person's assistant until
-- frozen_until (or until they clear it in the web app).
CREATE TABLE IF NOT EXISTS assistant_freezes (
    user_id      TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    frozen_until TEXT NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- ---- 20.5 cash flows ---------------------------------------------------------
-- Money moved into or out of a simulated book (deposits and withdrawals), so
-- returns are time and money weighted and a deposit is never profit. Broker
-- books read theirs from broker_activities.
CREATE TABLE IF NOT EXISTS portfolio_cash_flows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    flow_date    TEXT NOT NULL,                     -- ISO date
    kind         TEXT NOT NULL CHECK (kind IN ('deposit', 'withdrawal')),
    amount       REAL NOT NULL CHECK (amount > 0),  -- always positive, kind gives the sign
    note         TEXT,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_cash_flows ON portfolio_cash_flows(portfolio_id, flow_date);
