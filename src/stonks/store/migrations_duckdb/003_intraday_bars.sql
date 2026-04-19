-- Interval-aware bars. Supersedes the daily-only ``prices`` table; ``prices``
-- is recreated as a read-only view over the daily slice of ``bars`` so that
-- raw SQL in existing code paths keeps working.
--
-- The ``interval`` column holds the canonical Interval code ('1m', '5m',
-- '15m', '30m', '1h', '4h', '12h', '1d', '1w', …). Storing it on the row
-- lets a single table hold data at mixed granularities for the same ticker
-- (e.g. cached 1d bars alongside live 5m bars).

CREATE TABLE bars (
    ticker    VARCHAR   NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    interval  VARCHAR   NOT NULL,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    adj_close DOUBLE,
    volume    BIGINT,
    PRIMARY KEY (ticker, timestamp, interval)
);

-- Migrate existing daily rows into the new table at interval='1d'
INSERT INTO bars (ticker, timestamp, interval, open, high, low, close, adj_close, volume)
SELECT ticker, CAST(date AS TIMESTAMP), '1d',
       open, high, low, close, adj_close, volume
FROM prices;

DROP TABLE prices;

-- Recreate `prices` as a read-only view so SQL queries that still refer to it
-- keep working during the transition.
CREATE VIEW prices AS
SELECT ticker, CAST(timestamp AS DATE) AS date,
       open, high, low, close, adj_close, volume
FROM bars
WHERE interval = '1d';
