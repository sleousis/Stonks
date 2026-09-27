-- Model lifecycle (roadmap 22.6; docs/model-lifecycle.md).
--
-- A strategy id keeps one or more fitted model versions. Version 1 is the
-- fit the strategy was registered with. A scheduled retrain adds a
-- candidate version, which runs as a model book beside the live version.
-- A swap makes a candidate live, and only through
-- registry.versions.ModelVersionRegistry, which logs every change first.
--
-- model_versions          one row per fitted version. At most one is live.
-- model_version_events    append-only log: baseline, candidate, swap,
--                         reject, supersede and fail rows, with the actor,
--                         the reason and the swap check report.
-- model_version_decisions and model_version_snapshots
--                         the version model books (like shadow_decisions and
--                         shadow_portfolio_snapshots, keyed by version).

CREATE TABLE IF NOT EXISTS model_versions (
    strategy_id   TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL CHECK (version >= 1),
    -- relative to the artifacts folder, like strategies.artifact_path
    artifact_path TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN ('live', 'candidate', 'archived', 'rejected', 'failed')),
    train_start   TEXT,                       -- ISO date, NULL for the baseline
    train_end     TEXT,                       -- ISO date, NULL for the baseline
    fit_json      TEXT,                       -- JSON summary of the fit
    error         TEXT,                       -- why the fit failed (status failed)
    created_by    TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_model_versions_live
    ON model_versions(strategy_id) WHERE status = 'live';
CREATE INDEX IF NOT EXISTS idx_model_versions_status ON model_versions(status);

CREATE TABLE IF NOT EXISTS model_version_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id       TEXT NOT NULL,
    version           INTEGER NOT NULL,
    kind              TEXT NOT NULL
                      CHECK (kind IN ('baseline', 'candidate', 'swap', 'reject',
                                      'supersede', 'fail')),
    from_status       TEXT,
    to_status         TEXT NOT NULL,
    actor             TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    reason            TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    override          INTEGER NOT NULL DEFAULT 0 CHECK (override IN (0, 1)),
    check_passed      INTEGER CHECK (check_passed IN (0, 1)),   -- NULL: no report
    check_report_json TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_version_events_strategy
    ON model_version_events(strategy_id, version, id);

CREATE TRIGGER IF NOT EXISTS model_version_events_no_update
BEFORE UPDATE ON model_version_events
BEGIN
    SELECT RAISE(ABORT, 'model_version_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS model_version_events_no_delete
BEFORE DELETE ON model_version_events
BEGIN
    SELECT RAISE(ABORT, 'model_version_events is append-only');
END;

-- A new live row is only the baseline (the first version of a strategy).
CREATE TRIGGER IF NOT EXISTS model_versions_live_insert
BEFORE INSERT ON model_versions
WHEN NEW.status = 'live'
 AND EXISTS (SELECT 1 FROM model_versions WHERE strategy_id = NEW.strategy_id)
BEGIN
    SELECT RAISE(ABORT, 'a version becomes live through ModelVersionRegistry.swap (audited)');
END;

-- No unaudited version status change: the registry logs the event first,
-- then updates the row with updated_at = the event's created_at.
CREATE TRIGGER IF NOT EXISTS model_versions_status_audited
BEFORE UPDATE OF status ON model_versions
WHEN NEW.status IS NOT OLD.status AND NOT EXISTS (
    SELECT 1 FROM model_version_events e
    WHERE e.id = (
        SELECT MAX(id) FROM model_version_events
        WHERE strategy_id = NEW.strategy_id AND version = NEW.version
    )
      AND e.from_status = OLD.status
      AND e.to_status = NEW.status
      AND e.created_at = NEW.updated_at
)
BEGIN
    SELECT RAISE(ABORT, 'version changes go through ModelVersionRegistry (audited)');
END;

-- The model a strategy trades is its live version's artifact.
CREATE TRIGGER IF NOT EXISTS strategies_artifact_path_audited
BEFORE UPDATE OF artifact_path ON strategies
WHEN NEW.artifact_path IS NOT OLD.artifact_path AND NOT EXISTS (
    SELECT 1 FROM model_versions mv
    WHERE mv.strategy_id = NEW.id AND mv.status = 'live' AND mv.artifact_path = NEW.artifact_path
)
BEGIN
    SELECT RAISE(ABORT, 'the artifact changes only through ModelVersionRegistry.swap');
END;

CREATE TABLE IF NOT EXISTS model_version_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id     TEXT NOT NULL REFERENCES tick_runs(id),
    strategy_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    as_of       TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    REAL NOT NULL,
    price       REAL,
    status      TEXT NOT NULL CHECK (status IN ('filled', 'rejected')),
    created_at  TEXT NOT NULL,
    UNIQUE (strategy_id, version, as_of, ticker, side),
    FOREIGN KEY (strategy_id, version)
        REFERENCES model_versions(strategy_id, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_version_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id        TEXT NOT NULL REFERENCES tick_runs(id),
    strategy_id    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    as_of          TEXT NOT NULL,
    taken_at       TEXT NOT NULL,
    cash           REAL NOT NULL,
    positions_json TEXT NOT NULL,
    total_value    REAL NOT NULL,
    UNIQUE (strategy_id, version, as_of),
    FOREIGN KEY (strategy_id, version)
        REFERENCES model_versions(strategy_id, version) ON DELETE CASCADE
);
