-- Live marks and intraday P&L (roadmap 21.3.3, design docs/design/intraday.md).
--
-- One row per book every few minutes of a session, written by the intraday
-- P&L tracker (production/intraday_pnl.py) on the event engine. A book is a
-- whole portfolio (strategy_id = '') or one strategy's sleeve of it. A rerun
-- at the same moment replaces the row.
--
--   day                  the trading day (UTC date of the bar close)
--   at                   the bar close the row was computed at (UTC)
--   start_value          portfolio: cash plus positions at the prior close,
--                        sleeve: gross exposure at the prior close
--   value                portfolio: start_value plus pnl, sleeve: gross now
--   realised             P&L of fills that reduced a position (average cost)
--   unrealised           open positions at the latest marks against cost
--   fees                 fees of the day's fills
--   pnl                  realised plus unrealised less fees
--   day_return           pnl over start_value (NULL when that is zero)
--   high_water_pnl       the day's best pnl so far (zero or more)
--   drawdown             the drop from that high over the value there (<= 0)
--   gross_exposure, net_exposure, exposures_json   at the latest marks
--   fills                fills booked so far today
--   unmarked             held tickers with no mark yet (valued at cost)
--   stale_marks          held tickers whose mark is older than the limit
--   max_mark_age_seconds the oldest mark of a held ticker

CREATE TABLE IF NOT EXISTS intraday_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id         TEXT NOT NULL,
    strategy_id          TEXT NOT NULL DEFAULT '',
    day                  TEXT NOT NULL,
    at                   TEXT NOT NULL,
    start_value          REAL NOT NULL,
    value                REAL NOT NULL,
    realised             REAL NOT NULL DEFAULT 0,
    unrealised           REAL NOT NULL DEFAULT 0,
    fees                 REAL NOT NULL DEFAULT 0,
    pnl                  REAL NOT NULL DEFAULT 0,
    day_return           REAL,
    high_water_pnl       REAL NOT NULL DEFAULT 0 CHECK (high_water_pnl >= 0),
    drawdown             REAL NOT NULL DEFAULT 0 CHECK (drawdown <= 0),
    gross_exposure       REAL NOT NULL DEFAULT 0,
    net_exposure         REAL NOT NULL DEFAULT 0,
    exposures_json       TEXT NOT NULL DEFAULT '{}',
    fills                INTEGER NOT NULL DEFAULT 0,
    unmarked             INTEGER NOT NULL DEFAULT 0,
    stale_marks          INTEGER NOT NULL DEFAULT 0,
    max_mark_age_seconds REAL,
    created_at           TEXT NOT NULL,
    UNIQUE (portfolio_id, strategy_id, at)
);

CREATE INDEX IF NOT EXISTS idx_intraday_snapshots_day
    ON intraday_snapshots(portfolio_id, day, at);
