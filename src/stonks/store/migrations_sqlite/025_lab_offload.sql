-- Lab offload (roadmap 14.9): jobs a separate lab worker process runs.
-- executor: 'local' runs in the process that queued it (the API), 'worker'
-- waits in the queue for a lab worker (python -m stonks.lab.offload worker).
ALTER TABLE jobs ADD COLUMN executor TEXT NOT NULL DEFAULT 'local';
-- The worker that claimed the job and its last heartbeat. A running worker
-- job whose heartbeat is older than the lease is failed as "worker lost".
ALTER TABLE jobs ADD COLUMN worker_id TEXT;
ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT;
-- Set by a cancel of a running worker job; the worker polls it.
ALTER TABLE jobs ADD COLUMN cancel_requested_at TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_executor_status ON jobs(executor, status, created_at);

-- One row per lab worker process (health and metrics).
CREATE TABLE IF NOT EXISTS lab_workers (
    id              TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    pid             INTEGER NOT NULL,
    cpus            INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    heartbeat_at    TEXT NOT NULL,
    stopped_at      TEXT,
    current_job_id  TEXT,
    jobs_succeeded  INTEGER NOT NULL DEFAULT 0,
    jobs_failed     INTEGER NOT NULL DEFAULT 0,
    jobs_cancelled  INTEGER NOT NULL DEFAULT 0
);
