-- Roadmap 22.9: the AI research loop. The assistant proposes hypotheses and
-- runs lab trials under a budget. Every proposal is a row here before it
-- runs, and every lab run it starts is a normal ledgered lab run.

-- family: the research session a lab run belongs to (NULL for every other
-- run). A run is judged against the larger of its class's trial count and
-- its family's, so a search across classes counts every trial (P2).
ALTER TABLE lab_runs ADD COLUMN family TEXT;

CREATE INDEX IF NOT EXISTS idx_lab_runs_family ON lab_runs(family);

-- One research session: a person's goal, the model and its training cutoff,
-- the budgets it runs under and what it used.
CREATE TABLE IF NOT EXISTS research_sessions (
    id               TEXT PRIMARY KEY,               -- rs_<hex>
    owner_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    goal             TEXT NOT NULL,
    universe_json    TEXT NOT NULL DEFAULT '[]',     -- tickers, fixed for the session
    universe_id      TEXT,                           -- or a stored universe
    model            TEXT NOT NULL,
    model_cutoff     TEXT NOT NULL,                  -- ISO date: the model's training cutoff
    prompt_version   TEXT NOT NULL,
    max_trials       INTEGER NOT NULL CHECK (max_trials > 0),
    max_proposals    INTEGER NOT NULL CHECK (max_proposals > 0),
    max_cpu_seconds  REAL NOT NULL CHECK (max_cpu_seconds > 0),
    trials_used      INTEGER NOT NULL DEFAULT 0,
    cpu_seconds_used REAL NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'stopped', 'failed')),
    stop_reason      TEXT,
    summary          TEXT,
    job_id           TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_research_sessions_owner
    ON research_sessions(owner_id, created_at);

-- Each proposal the model made, recorded with its hypothesis before it runs.
-- A rejected proposal (bad input, over budget, validation before the model's
-- cutoff, asks to register) never runs and keeps the reason.
CREATE TABLE IF NOT EXISTS research_proposals (
    id               TEXT PRIMARY KEY,               -- rp_<hex>
    session_id       TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    hypothesis       TEXT,
    premortem        TEXT,
    class_path       TEXT,
    arguments_json   TEXT NOT NULL DEFAULT '{}',     -- what the model proposed
    status           TEXT NOT NULL
                     CHECK (status IN ('rejected', 'running', 'done', 'failed', 'stopped')),
    reason           TEXT,
    validation_start TEXT,                           -- ISO date the out-of-sample window starts
    budget           INTEGER,
    lab_run_id       TEXT,
    verdict          TEXT,
    best_score       REAL,
    trials           INTEGER NOT NULL DEFAULT 0,
    cpu_seconds      REAL NOT NULL DEFAULT 0,
    outcome_json     TEXT,
    created_at       TEXT NOT NULL,
    finished_at      TEXT,
    UNIQUE (session_id, seq)
);
