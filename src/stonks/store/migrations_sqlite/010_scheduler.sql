-- Built-in scheduler (roadmap 12.2-12.3). One row per scheduled run; the
-- UNIQUE (job_name, run_key) claim is what makes a fire run at most once,
-- across restarts, catch-up and a second scheduler process alike. run_key
-- is the session / local date for date-based triggers and the UTC instant
-- for interval triggers. Kept apart from the API's `jobs` table, whose rows
-- have no natural idempotency key and whose startup recovery belongs to
-- the API process.
CREATE TABLE IF NOT EXISTS scheduled_runs (
    id            TEXT PRIMARY KEY,
    job_name      TEXT NOT NULL,
    action        TEXT NOT NULL,
    run_key       TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,                 -- ISO-8601 UTC
    as_of         TEXT,                          -- the date the run is for
    status        TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'skipped', 'failed')),
    catch_up      INTEGER NOT NULL DEFAULT 0 CHECK (catch_up IN (0, 1)),
    actor         TEXT NOT NULL DEFAULT 'service:scheduler',
    instance_id   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    detail_json   TEXT,
    error         TEXT,
    UNIQUE (job_name, run_key)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_runs_job ON scheduled_runs(job_name, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_status ON scheduled_runs(status);

-- One row per scheduler process start; heartbeat_at backs liveness.
CREATE TABLE IF NOT EXISTS scheduler_instances (
    id           TEXT PRIMARY KEY,
    host         TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    stopped_at   TEXT
);

-- Deadline alerts already sent, so the watchdog alerts once per missed run
-- even across restarts.
CREATE TABLE IF NOT EXISTS scheduler_deadline_alerts (
    job_name   TEXT NOT NULL,
    run_key    TEXT NOT NULL,
    alerted_at TEXT NOT NULL,
    PRIMARY KEY (job_name, run_key)
);
