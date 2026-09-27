-- Roadmap 20.7: event calendars.
--
-- The calendars are vendor neutral: every DataSource that serves them fills
-- the same columns with the same normalized values.
--
-- earnings_calendar: one row per company and fiscal period. The report date
-- can move while the period stays, so the period is the key.
-- before_after_market is normalized to 'before' | 'during' | 'after' (NULL
-- when the vendor does not say), as in earnings_announcements.
CREATE TABLE earnings_calendar (
    ticker              VARCHAR   NOT NULL,
    period_end          DATE      NOT NULL,
    report_date         DATE      NOT NULL,
    before_after_market VARCHAR,
    currency            VARCHAR,
    eps_estimate        DOUBLE,
    eps_actual          DOUBLE,
    eps_difference      DOUBLE,
    surprise_percent    DOUBLE,
    source              VARCHAR   NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, period_end)
);

-- dividend_calendar: upcoming and recent ex-dividend dates. Some vendors
-- list only the date, so the amount and the other dates may be NULL; an
-- upsert never overwrites a stored value with NULL.
CREATE TABLE dividend_calendar (
    ticker           VARCHAR   NOT NULL,
    ex_date          DATE      NOT NULL,
    amount           DOUBLE,
    currency         VARCHAR,
    record_date      DATE,
    pay_date         DATE,
    declaration_date DATE,
    source           VARCHAR   NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, ex_date)
);

-- economic_events: scheduled macro releases (CPI, payrolls, rate decisions).
-- country is ISO 3166-1 alpha-2 or a region code such as EU. event_time is
-- UTC. comparison is 'mom' | 'qoq' | 'yoy' | 'none'.
CREATE TABLE economic_events (
    country     VARCHAR   NOT NULL,
    event_time  TIMESTAMP NOT NULL,
    event_type  VARCHAR   NOT NULL,
    comparison  VARCHAR   NOT NULL,
    period      VARCHAR,
    actual      DOUBLE,
    previous    DOUBLE,
    estimate    DOUBLE,
    change      DOUBLE,
    change_pct  DOUBLE,
    source      VARCHAR   NOT NULL,
    updated_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (country, event_time, event_type, comparison)
);
