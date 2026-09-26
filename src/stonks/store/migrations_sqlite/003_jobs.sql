-- Background jobs for long operations (backtests, lab runs, ingest, ticks)
-- run by the in-process JobRunner behind the REST API (Phase 5.1).
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,              -- e.g. backtest | lab_run | ingest | tick
    params_json      TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    progress         REAL NOT NULL DEFAULT 0,    -- 0..1
    progress_message TEXT,
    result_json      TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
