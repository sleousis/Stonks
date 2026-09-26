-- Data quality on ingest (roadmap 12.5).
--
-- quarantined_bars: bars a validator rejected, kept out of the bar store
-- with the reason codes (comma-joined, see stonks.ingest.quality) and the
-- source that supplied them. An audit log, so no natural key: a bar
-- rejected again by a later run gets another row.
--
-- ingest_runs.quality_json: the run's quality summary (bars checked and
-- quarantined, reason and warning counts, and which tickers a fallback
-- source supplied). The bars themselves stay vendor-agnostic; provenance
-- lives here and on quarantined rows.
CREATE SEQUENCE IF NOT EXISTS quarantined_bars_id_seq;

CREATE TABLE quarantined_bars (
    id             BIGINT    PRIMARY KEY DEFAULT nextval('quarantined_bars_id_seq'),
    run_id         INTEGER,
    ticker         VARCHAR   NOT NULL,
    timestamp      TIMESTAMP NOT NULL,
    interval       VARCHAR   NOT NULL,
    open           DOUBLE,
    high           DOUBLE,
    low            DOUBLE,
    close          DOUBLE,
    adj_close      DOUBLE,
    volume         BIGINT,
    reasons        VARCHAR   NOT NULL,
    source         VARCHAR,
    quarantined_at TIMESTAMP NOT NULL
);

ALTER TABLE ingest_runs ADD COLUMN quality_json VARCHAR;
