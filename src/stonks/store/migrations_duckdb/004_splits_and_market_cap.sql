-- Stock splits (corporate actions) and historical market capitalization.

CREATE TABLE stock_splits (
    ticker   VARCHAR NOT NULL,
    date     DATE    NOT NULL,
    -- "to-for" ratio. 2.0 means "2 new shares for every 1 old" (2:1 forward
    -- split). 0.5 means a 1-for-2 reverse split.
    ratio    DOUBLE  NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE market_cap_history (
    ticker      VARCHAR NOT NULL,
    date        DATE    NOT NULL,
    market_cap  DOUBLE  NOT NULL,
    PRIMARY KEY (ticker, date)
);
