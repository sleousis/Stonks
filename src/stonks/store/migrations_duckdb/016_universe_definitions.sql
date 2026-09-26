-- Dynamic universes and on-demand data (roadmap 10.5).
--
-- universe_definitions: a named universe as a stored object. kind picks the
-- provider that turns spec_json into universe_membership spans (migration
-- 015): 'list' (static tickers or dated spans), 'exchange' (every symbol a
-- data source lists on an exchange, delisted ones included), 'rule'
-- (point-in-time filters evaluated on the lake at each rebalance date) and
-- 'index' (constituents rebuilt from a change history). A refresh replaces
-- the universe's membership rows, so re-running it is idempotent.
CREATE TABLE universe_definitions (
    id            VARCHAR   PRIMARY KEY,
    kind          VARCHAR   NOT NULL CHECK (kind IN ('list', 'exchange', 'rule', 'index')),
    name          VARCHAR,
    description   VARCHAR,
    spec_json     VARCHAR   NOT NULL,
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL,
    refreshed_at  TIMESTAMP,
    member_count  INTEGER
);

-- index_constituent_snapshots: the members of an index on one day (a
-- vendor's current table, or an imported list). index_id is a canonical
-- lower_snake_case code such as 'sp500'.
CREATE TABLE index_constituent_snapshots (
    index_id  VARCHAR NOT NULL,
    as_of     DATE    NOT NULL,
    ticker    VARCHAR NOT NULL,
    source    VARCHAR,
    PRIMARY KEY (index_id, as_of, ticker)
);

-- index_constituent_changes: one row per addition to or removal from an
-- index. Together with a snapshot this rebuilds membership on any past day.
CREATE TABLE index_constituent_changes (
    index_id     VARCHAR NOT NULL,
    ticker       VARCHAR NOT NULL,
    change_date  DATE    NOT NULL,
    action       VARCHAR NOT NULL CHECK (action IN ('add', 'remove')),
    source       VARCHAR,
    PRIMARY KEY (index_id, ticker, change_date, action)
);

-- bar_fetch_ranges: the date ranges a data source was already asked for,
-- per ticker and interval, whether or not it returned rows. The data
-- ensurer subtracts them from its gaps so a ticker with no data (a dead
-- name, a vendor gap) is not fetched again on every run.
CREATE TABLE bar_fetch_ranges (
    ticker       VARCHAR   NOT NULL,
    interval     VARCHAR   NOT NULL,
    range_start  DATE      NOT NULL,
    range_end    DATE      NOT NULL,
    source       VARCHAR,
    rows         INTEGER,
    fetched_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, interval, range_start, range_end),
    CHECK (range_end >= range_start)
);
