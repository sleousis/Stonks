-- Trial ledger (BL-04): every lab run with its pre-registration
-- (hypothesis, premortem) and every tuning trial it evaluated, so trial
-- counts survive across runs and the deflated Sharpe / PBO tests can see
-- them. Written only by stonks.lab.trials.TrialLedger. Per-bar trial
-- returns live beside the artifacts in data/artifacts/_trials/<run_id>.npz.
CREATE TABLE IF NOT EXISTS lab_runs (
    id             TEXT PRIMARY KEY,           -- lab_<utc timestamp>_<hex>, sortable
    strategy_class TEXT NOT NULL,              -- "module:Class"
    hypothesis     TEXT,                       -- recorded before tuning starts
    premortem      TEXT,
    tuner          TEXT,
    objective      TEXT,
    budget         INTEGER,
    seed           INTEGER,
    dataset_json   TEXT NOT NULL DEFAULT '{}', -- universe, windows, interval
    manifest_json  TEXT NOT NULL DEFAULT '{}', -- reproducibility manifest (BL-06)
    started_at     TEXT NOT NULL,              -- ISO-8601 UTC
    finished_at    TEXT,
    verdict        TEXT CHECK (verdict IS NULL OR verdict IN ('pass', 'fail', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_lab_runs_class ON lab_runs(strategy_class, started_at);

CREATE TABLE IF NOT EXISTS lab_trials (
    run_id      TEXT NOT NULL REFERENCES lab_runs(id) ON DELETE CASCADE,
    trial_index INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    score       REAL,                          -- NULL when the trial failed (NaN)
    n_bars      INTEGER,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    PRIMARY KEY (run_id, trial_index)
);
