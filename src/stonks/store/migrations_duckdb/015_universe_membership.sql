-- Point-in-time universe membership (BL-37, principle P14).
--
-- universe_membership: which tickers belonged to a named universe (an index,
-- a watch list, a screen) and when. A ticker is a member on day d when
-- start_date <= d and (end_date IS NULL or d < end_date). end_date is the
-- first day the ticker is no longer a member, so back-to-back spans never
-- overlap. A ticker that left and came back has one row per span.
-- Delisted names keep their rows, which is the point: a backtest that reads
-- membership as of each date sees the names that later died.
CREATE TABLE universe_membership (
    universe_id VARCHAR NOT NULL,
    ticker      VARCHAR NOT NULL,
    start_date  DATE    NOT NULL,
    end_date    DATE,
    PRIMARY KEY (universe_id, ticker, start_date),
    CHECK (end_date IS NULL OR end_date > start_date)
);
