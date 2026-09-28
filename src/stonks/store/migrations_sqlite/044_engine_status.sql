-- Live engine status (roadmap 21.3.4, design docs/design/intraday.md).
--
-- engine_status   one row per live engine, written by its EngineMonitor.
--                 The engine runs in its own process, so the API reads
--                 this row for /metrics and GET /api/stream/status, and
--                 the scheduler's watchdog reads it for the engine
--                 dead-man. snapshot_json holds the stream health, the
--                 driver counters and the latency histograms.
--
-- The dead-man alerts once per silent stretch through the existing
-- scheduler_deadline_alerts table (job_name 'engine:<engine_id>').

CREATE TABLE IF NOT EXISTS engine_status (
    engine_id        TEXT PRIMARY KEY CHECK (length(trim(engine_id)) > 0),
    -- the market calendar the dead-man checks (XNYS, 24/7, ...)
    calendar         TEXT NOT NULL,
    state            TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    -- NULL while the engine runs
    stopped_at       TEXT,
    -- the last bar close the driver dispatched, NULL before the first
    last_dispatch_at TEXT,
    snapshot_json    TEXT NOT NULL DEFAULT '{}'
);
