-- The tuner's winner, per-trial seeds and a heartbeat for lab runs.
--
-- `best_trial_index` and `best_params_json` hold the trial the tuner chose
-- (never a heatmap cell), written when the run finishes. `heartbeat_at` is
-- touched while a run is alive, so the reaper only closes runs that stopped
-- beating past the offload lease. `lab_trials.seed` is the seed a trial's
-- RNGs ran with, NULL when it ran unseeded or the tuner did not say.

ALTER TABLE lab_runs ADD COLUMN best_trial_index INTEGER;
ALTER TABLE lab_runs ADD COLUMN best_params_json TEXT;
ALTER TABLE lab_runs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE lab_trials ADD COLUMN seed INTEGER;

CREATE INDEX IF NOT EXISTS idx_lab_runs_open ON lab_runs(started_at) WHERE verdict IS NULL;
