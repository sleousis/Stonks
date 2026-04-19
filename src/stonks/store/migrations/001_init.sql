CREATE TABLE IF NOT EXISTS tickers (
    id          VARCHAR PRIMARY KEY,
    exchange    VARCHAR,
    currency    VARCHAR,
    ipo_date    DATE,
    sector      VARCHAR,
    industry    VARCHAR,
    is_delisted BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS prices (
    ticker    VARCHAR NOT NULL,
    date      DATE    NOT NULL,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    adj_close DOUBLE,
    volume    BIGINT,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker     VARCHAR NOT NULL,
    period_end DATE    NOT NULL,
    frequency  VARCHAR NOT NULL,
    statement  VARCHAR NOT NULL,
    line_item  VARCHAR NOT NULL,
    value      DOUBLE,
    PRIMARY KEY (ticker, period_end, frequency, statement, line_item)
);

CREATE SEQUENCE IF NOT EXISTS ingest_runs_id_seq;

CREATE TABLE IF NOT EXISTS ingest_runs (
    id             INTEGER PRIMARY KEY DEFAULT nextval('ingest_runs_id_seq'),
    source         VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,
    started_at     TIMESTAMP NOT NULL,
    finished_at    TIMESTAMP,
    tickers_ok     INTEGER DEFAULT 0,
    tickers_failed INTEGER DEFAULT 0,
    status         VARCHAR,
    error          VARCHAR
);
