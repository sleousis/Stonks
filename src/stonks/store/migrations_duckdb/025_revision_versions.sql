-- Versioned macro prints and fund holdings (principle P12).
--
-- Vendors revise macro observations and fund holdings in place. A plain
-- upsert then shows the revised value at decisions made before Stonks saw
-- it, and a holding that drops out of a re-fetched fund snapshot stays in
-- it. Like the statement versions of migration 018, each table gets a
-- <table>_versions twin: the same columns plus known_at, the time Stonks
-- first saw that version (naive UTC). The current tables stay the latest
-- view.
--
-- macro_indicators_versions: a decision reads, per (country_iso,
-- indicator, observation_date), the latest version seen by then. The
-- first version is the one as published, so it counts from its
-- publication date whenever it was seen (history ingested late stays
-- usable). Existing rows are backfilled stamped with observation_date.
--
-- fund_holdings_versions: removed marks a holding that dropped out of a
-- re-fetched (fund, as_of, source) snapshot. A decision reads, per
-- holding, the latest version known by then and skips removed ones.
-- Existing rows are backfilled with their known_at.
--
-- A later migration that adds a column to one of these tables must add it
-- to its _versions twin too.

CREATE TABLE macro_indicators_versions AS
    SELECT *, CAST(observation_date AS TIMESTAMP) AS known_at
      FROM macro_indicators;
ALTER TABLE macro_indicators_versions ALTER COLUMN known_at SET NOT NULL;

CREATE TABLE fund_holdings_versions (
    fund      VARCHAR   NOT NULL,
    holding   VARCHAR   NOT NULL,
    as_of     DATE      NOT NULL,
    source    VARCHAR   NOT NULL,
    weight    DOUBLE    NOT NULL,   -- the last weight when removed
    name      VARCHAR,
    sector    VARCHAR,
    country   VARCHAR,
    known_at  TIMESTAMP NOT NULL,
    removed   BOOLEAN   NOT NULL DEFAULT false,
    PRIMARY KEY (fund, holding, as_of, source, known_at)
);

INSERT INTO fund_holdings_versions
    (fund, holding, as_of, source, weight, name, sector, country, known_at, removed)
SELECT fund, holding, as_of, source, weight, name, sector, country, known_at, false
  FROM fund_holdings;
