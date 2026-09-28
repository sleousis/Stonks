-- Roadmap 23.9: live calibration of classifier model versions.
--
-- While a strategy's model versions run as model books, each version that
-- forecasts a probability (a classifier's P(win)) records one row per
-- forecast event. A later tick resolves it: outcome 1 when the event
-- happened, 0 when not. The Brier score and the reliability table per
-- version are computed from these rows. One row per event, so a forecast
-- repeated on the days a trade stays open counts once.

CREATE TABLE model_forecasts (
    strategy_id  TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    ticker       TEXT NOT NULL,
    event_key    TEXT NOT NULL,              -- the event the forecast is about, e.g. an entry time
    as_of        TEXT NOT NULL,              -- ISO date of the first forecast of the event
    probability  REAL NOT NULL CHECK (probability >= 0 AND probability <= 1),
    outcome      INTEGER CHECK (outcome IN (0, 1)),   -- NULL until resolved
    resolved_on  TEXT,                       -- ISO date the outcome was known
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version, ticker, event_key)
);

CREATE INDEX model_forecasts_open ON model_forecasts (strategy_id, version, outcome);
