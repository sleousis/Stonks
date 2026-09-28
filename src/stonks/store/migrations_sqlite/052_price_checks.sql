-- Second-source price check (roadmap 23.6).
--
-- price_checks   one row per run of the price check before the tick: the
--                vendor's latest closes and adjusted returns of held and
--                signalled tickers against a second source.
--   status       clean (no gap), gaps (some tickers gapped: their opening
--                orders are held), systematic (most compared tickers
--                gapped: the global operational halt opened) or
--                unavailable (nothing could be compared).
--   held_json    the tickers whose opening orders the tick holds on as_of.
--   items_json   per ticker: vendor and second close, the gaps and why.
--   halt_id      the operational halt a systematic gap opened.
-- Rows are written once and never changed. The tick and Health read the
-- newest row of a day.

CREATE TABLE IF NOT EXISTS price_checks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of            TEXT NOT NULL,
    checked_at       TEXT NOT NULL,
    source           TEXT NOT NULL,
    status           TEXT NOT NULL
                     CHECK (status IN ('clean', 'gaps', 'systematic', 'unavailable')),
    tickers_checked  INTEGER NOT NULL DEFAULT 0,
    tickers_compared INTEGER NOT NULL DEFAULT 0,
    held_json        TEXT NOT NULL DEFAULT '[]',
    items_json       TEXT NOT NULL DEFAULT '[]',
    detail           TEXT,
    halt_id          INTEGER REFERENCES risk_halts(id)
);

CREATE INDEX IF NOT EXISTS idx_price_checks_as_of ON price_checks(as_of, id DESC);
