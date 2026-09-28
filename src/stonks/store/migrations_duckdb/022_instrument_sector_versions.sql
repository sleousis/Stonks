-- Point-in-time sector labels (principle P12, roadmap 22.10).
--
-- instruments.sector and gic_sector are overwritten when a vendor
-- reclassifies a company. A report over the past would then group old
-- returns by today's label.
--
-- instrument_sector_versions keeps every label an instrument has had, with
-- known_at, the time Stonks first saw it (naive UTC). Each profile upsert
-- that changes a sector appends a version.
--
-- A read on a day takes the latest version known by then. The first version
-- of a ticker counts on every earlier day too: it is the oldest label
-- Stonks has, and without it the name would have no sector at all.
--
-- Existing labels are backfilled as first versions, stamped at the epoch.

CREATE TABLE instrument_sector_versions (
    ticker      VARCHAR   NOT NULL,
    sector      VARCHAR,
    gic_sector  VARCHAR,
    known_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, known_at)
);

INSERT INTO instrument_sector_versions (ticker, sector, gic_sector, known_at)
    SELECT id, sector, gic_sector, TIMESTAMP '1970-01-01 00:00:00'
      FROM instruments
     WHERE sector IS NOT NULL OR gic_sector IS NOT NULL;
