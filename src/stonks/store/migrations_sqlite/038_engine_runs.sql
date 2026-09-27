-- Intraday engine runs (roadmap 21.2.5, docs/design/intraday.md section 9).
--
-- One row per start of the engine process. The process writes its
-- heartbeat and checkpoint here after every bar close it handled, so a
-- restart after a crash knows where the last run stopped.
--
-- status          running while the process is up. stopped after a clean
--                 stop, failed after an error it caught, crashed when a
--                 later start found the row still running with no process.
-- last_close_at   the last bar close whose orders were routed (the
--                 checkpoint). A replay that resumes skips closes up to it.
-- recovered_from  the crashed run of the same session this run resumed.

CREATE TABLE IF NOT EXISTS engine_runs (
    id              TEXT PRIMARY KEY,
    session_date    TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('live', 'replay')),
    status          TEXT NOT NULL
                    CHECK (status IN ('running', 'stopped', 'failed', 'crashed')),
    pid             INTEGER,
    started_at      TEXT NOT NULL,
    heartbeat_at    TEXT,
    finished_at     TEXT,
    last_close_at   TEXT,
    bar_closes      INTEGER NOT NULL DEFAULT 0,
    orders_routed   INTEGER NOT NULL DEFAULT 0,
    recovered_from  TEXT REFERENCES engine_runs(id),
    error           TEXT,
    summary_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_engine_runs_session ON engine_runs(session_date, started_at);
CREATE INDEX IF NOT EXISTS idx_engine_runs_status ON engine_runs(status);
