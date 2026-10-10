-- Roadmap 19.11: a record of every kill switch drill.
--
-- `stonks halts drill` runs the drill on a scratch state DB, so nothing in
-- the drill itself touches production. Each run then leaves one row here in
-- the real state DB. The live_small and live_scale stage gates need a
-- passing drill within `[production.live.stages] drill_max_age_days`.
-- portfolio_id is NULL for a drill of the global kill switch, which counts
-- for every portfolio.

CREATE TABLE kill_switch_drills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at        TEXT NOT NULL,             -- ISO time the drill finished (UTC)
    ran_by        TEXT NOT NULL,             -- who ran it, e.g. cli:<os user>
    portfolio_id  TEXT,                      -- NULL: a global drill, counts for all
    broker        TEXT NOT NULL,             -- the drill broker, never a real one
    passed        INTEGER NOT NULL CHECK (passed IN (0, 1)),
    report_json   TEXT NOT NULL              -- DrillReport.to_dict()
);

CREATE INDEX kill_switch_drills_passed ON kill_switch_drills (passed, ran_at);
