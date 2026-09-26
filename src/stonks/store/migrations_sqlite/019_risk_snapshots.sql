-- Live risk monitoring (BL-47, roadmap 9.5.4; principles P17, P29, P38).
--
-- One row per book per day, written by the risk_monitor tick hook after
-- every real tick. A book is a whole portfolio (strategy_id = '') or one
-- strategy's sleeve of it (the positions position_attribution gives it).
-- A rerun of the same day replaces the row.
--
--   value              portfolio: cash plus positions; sleeve: gross exposure
--   exposures_json     currency exposure per ticker at the day's marks
--   sigma, var_*, es_* one-day forecast from an EWMA covariance, as fractions
--                      of value, a loss is positive
--   observations       daily return rows behind the forecast
--   realized_return    yesterday's exposures times today's returns, over
--                      yesterday's value (pnl is the same in currency)
--   violation_*        realized loss beyond yesterday's VaR (NULL on day one)
--   window_days        scored days in the rolling window (up to 250)
--   violation_ratio_*  violations over the expected count (1.0 is right)
--   kupiec_p_*         Kupiec proportion-of-failures p-value
--   ir_short, ir_long  annualised rolling 60 and 120 day IR of the sleeve
--   expected_ir        what the backtest promised (oos Sharpe, else the
--                      benchmark-relative IR)
--   decay_days         trailing days the short IR has stayed below zero
--   decayed            the alpha-decay monitor fired on this day

CREATE TABLE IF NOT EXISTS risk_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of               TEXT NOT NULL,
    tick_id             TEXT,
    portfolio_id        TEXT NOT NULL,
    strategy_id         TEXT NOT NULL DEFAULT '',
    value               REAL NOT NULL,
    exposures_json      TEXT NOT NULL DEFAULT '{}',
    sigma               REAL,
    var_95              REAL,
    var_99              REAL,
    es_95               REAL,
    es_99               REAL,
    observations        INTEGER NOT NULL DEFAULT 0,
    realized_return     REAL,
    pnl                 REAL,
    violation_95        INTEGER,
    violation_99        INTEGER,
    window_days         INTEGER NOT NULL DEFAULT 0,
    violations_95       INTEGER NOT NULL DEFAULT 0,
    violations_99       INTEGER NOT NULL DEFAULT 0,
    violation_ratio_95  REAL,
    violation_ratio_99  REAL,
    kupiec_p_95         REAL,
    kupiec_p_99         REAL,
    ir_short            REAL,
    ir_long             REAL,
    expected_ir         REAL,
    decay_days          INTEGER,
    decayed             INTEGER NOT NULL DEFAULT 0,
    decay_reason        TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (portfolio_id, strategy_id, as_of)
);

CREATE INDEX IF NOT EXISTS idx_risk_snapshots_day ON risk_snapshots(as_of);
