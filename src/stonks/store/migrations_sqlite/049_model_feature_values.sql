-- Roadmap 23.10: the live input rows of model strategies, one per strategy,
-- training profile, day and ticker. The feature_drift tick hook compares the
-- recent rows with the training profile stored with the model (PSI per
-- feature, warn only). profile_id is a digest of that profile, so a new
-- model version starts a new window. Diagnostic data: no foreign key, and a
-- re-run of the same day replaces its rows.

CREATE TABLE model_feature_values (
    strategy_id TEXT NOT NULL,
    profile_id  TEXT NOT NULL,
    as_of       TEXT NOT NULL,   -- ISO date of the tick
    ticker      TEXT NOT NULL,
    values_json TEXT NOT NULL,   -- feature name to value, in training order
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, profile_id, as_of, ticker)
);
