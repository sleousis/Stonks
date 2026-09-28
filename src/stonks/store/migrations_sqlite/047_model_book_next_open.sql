-- Principle P21: model books fill like the backtest, at the next session's
-- open. A model book's decision is written as 'working' and the next tick
-- fills it ('filled', with filled_on the day of the open it filled at) or
-- lets it lapse ('expired'). 'filled' and 'rejected' keep their meaning for
-- rows written before (filled or refused at the latest close).
--
-- SQLite cannot change a CHECK, so shadow_decisions and
-- model_version_decisions are rebuilt with their rows and indexes. Nothing
-- references them.

CREATE TABLE shadow_decisions_047 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id     TEXT NOT NULL REFERENCES tick_runs(id),
    strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    as_of       TEXT NOT NULL,                -- ISO date the decision was made for
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    REAL NOT NULL,                -- filled quantity, else as decided
    price       REAL,                         -- simulated fill price, else the decision's close
    status      TEXT NOT NULL CHECK (status IN ('filled', 'rejected', 'working', 'expired')),
    created_at  TEXT NOT NULL,
    filled_on   TEXT,                         -- ISO date of the open it filled at
    UNIQUE (strategy_id, as_of, ticker, side)
);

INSERT INTO shadow_decisions_047 (id, tick_id, strategy_id, as_of, ticker, side, quantity, price,
    status, created_at, filled_on)
SELECT id, tick_id, strategy_id, as_of, ticker, side, quantity, price, status, created_at,
       CASE WHEN status = 'filled' THEN as_of END
  FROM shadow_decisions;

DROP INDEX IF EXISTS idx_shadow_decisions_strategy;
DROP TABLE shadow_decisions;
ALTER TABLE shadow_decisions_047 RENAME TO shadow_decisions;
CREATE INDEX IF NOT EXISTS idx_shadow_decisions_strategy ON shadow_decisions(strategy_id, as_of);
CREATE INDEX IF NOT EXISTS idx_shadow_decisions_working
    ON shadow_decisions(strategy_id, status);

CREATE TABLE model_version_decisions_047 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id     TEXT NOT NULL REFERENCES tick_runs(id),
    strategy_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    as_of       TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    REAL NOT NULL,
    price       REAL,
    status      TEXT NOT NULL CHECK (status IN ('filled', 'rejected', 'working', 'expired')),
    created_at  TEXT NOT NULL,
    filled_on   TEXT,
    UNIQUE (strategy_id, version, as_of, ticker, side),
    FOREIGN KEY (strategy_id, version)
        REFERENCES model_versions(strategy_id, version) ON DELETE CASCADE
);

INSERT INTO model_version_decisions_047 (id, tick_id, strategy_id, version, as_of, ticker, side,
    quantity, price, status, created_at, filled_on)
SELECT id, tick_id, strategy_id, version, as_of, ticker, side, quantity, price, status,
       created_at, CASE WHEN status = 'filled' THEN as_of END
  FROM model_version_decisions;

DROP TABLE model_version_decisions;
ALTER TABLE model_version_decisions_047 RENAME TO model_version_decisions;
CREATE INDEX IF NOT EXISTS idx_model_version_decisions_working
    ON model_version_decisions(strategy_id, version, status);
