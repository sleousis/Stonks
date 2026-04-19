-- Extended fundamentals surface — beyond the three financial statements.
-- Covers dividends, insider transactions, news + sentiment, analyst
-- estimates + ratings, shares outstanding, employee count, and revenue /
-- geographic segmentations. Plus a broader static profile per ticker.

-- Static profile additions. The `tickers` table already exists from
-- migration 001; widen it here. DuckDB supports ALTER TABLE ADD COLUMN.
ALTER TABLE tickers ADD COLUMN name VARCHAR;
ALTER TABLE tickers ADD COLUMN country_iso VARCHAR;
ALTER TABLE tickers ADD COLUMN fiscal_year_end VARCHAR;
ALTER TABLE tickers ADD COLUMN web_url VARCHAR;
ALTER TABLE tickers ADD COLUMN is_bank BOOLEAN DEFAULT FALSE;
ALTER TABLE tickers ADD COLUMN beta DOUBLE;
ALTER TABLE tickers ADD COLUMN short_percent DOUBLE;
ALTER TABLE tickers ADD COLUMN insider_ownership_percent DOUBLE;
ALTER TABLE tickers ADD COLUMN institutional_ownership_percent DOUBLE;
ALTER TABLE tickers ADD COLUMN employee_count INTEGER;
ALTER TABLE tickers ADD COLUMN esg_score DOUBLE;

-- Dividends (time-series)
CREATE TABLE dividends (
    ticker           VARCHAR NOT NULL,
    ex_date          DATE    NOT NULL,
    amount           DOUBLE  NOT NULL,
    currency         VARCHAR,
    pay_date         DATE,
    record_date      DATE,
    declaration_date DATE,
    PRIMARY KEY (ticker, ex_date)
);

-- Insider transactions (event-log)
CREATE SEQUENCE insider_transactions_id_seq;
CREATE TABLE insider_transactions (
    id               INTEGER PRIMARY KEY DEFAULT nextval('insider_transactions_id_seq'),
    ticker           VARCHAR NOT NULL,
    date             DATE    NOT NULL,
    owner_name       VARCHAR,
    owner_relation   VARCHAR,
    transaction_code VARCHAR,
    shares           DOUBLE,
    price            DOUBLE,
    value            DOUBLE,
    vendor_id        VARCHAR
);
-- Natural dedup key: same owner + code + shares on same day is the same trade.
CREATE UNIQUE INDEX uq_insider_natural ON insider_transactions (
    ticker, date, owner_name, transaction_code, shares
);

-- News articles (event-log)
CREATE TABLE news (
    ticker        VARCHAR NOT NULL,
    published_at  TIMESTAMP NOT NULL,
    title         VARCHAR NOT NULL,
    url           VARCHAR,
    source_name   VARCHAR,
    sentiment     DOUBLE,
    PRIMARY KEY (ticker, published_at, title)
);

-- Daily aggregate sentiment
CREATE TABLE news_sentiment (
    ticker        VARCHAR NOT NULL,
    date          DATE    NOT NULL,
    sentiment     DOUBLE,
    article_count INTEGER,
    PRIMARY KEY (ticker, date)
);

-- Per-metric analyst estimates (epsActual, epsEstimate, revenueEstimate, ...)
CREATE TABLE analyst_estimates (
    ticker     VARCHAR NOT NULL,
    period_end DATE    NOT NULL,
    metric     VARCHAR NOT NULL,
    value      DOUBLE,
    PRIMARY KEY (ticker, period_end, metric)
);

-- Current consensus snapshot. One row per ticker, upserted.
CREATE TABLE analyst_ratings (
    ticker       VARCHAR PRIMARY KEY,
    rating       DOUBLE,
    target_price DOUBLE,
    strong_buy   INTEGER DEFAULT 0,
    buy          INTEGER DEFAULT 0,
    hold         INTEGER DEFAULT 0,
    sell         INTEGER DEFAULT 0,
    strong_sell  INTEGER DEFAULT 0,
    updated_at   TIMESTAMP
);

-- Historical shares outstanding
CREATE TABLE shares_outstanding (
    ticker VARCHAR NOT NULL,
    date   DATE    NOT NULL,
    shares DOUBLE  NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- Historical employee count
CREATE TABLE employee_count (
    ticker VARCHAR NOT NULL,
    date   DATE    NOT NULL,
    count  INTEGER NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- Revenue + geographic segmentations (same shape, distinguished by dimension)
CREATE TABLE segmentation (
    ticker     VARCHAR NOT NULL,
    period_end DATE    NOT NULL,
    dimension  VARCHAR NOT NULL,      -- 'revenue' | 'geographic'
    segment    VARCHAR NOT NULL,
    value      DOUBLE,
    PRIMARY KEY (ticker, period_end, dimension, segment)
);
