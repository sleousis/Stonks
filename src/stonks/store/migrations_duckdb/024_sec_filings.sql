-- Primary-source filings, point in time (roadmap 23.13, principle P12).
--
-- insider_transactions.known_at: when the filing that reports the trade
-- was accepted by the regulator (naive UTC). A source that knows it (SEC
-- EDGAR's acceptance time) fills it. NULL for rows that only carry a
-- filing date; point-in-time reads then use the day after the filing date.
ALTER TABLE insider_transactions ADD COLUMN known_at TIMESTAMP;

-- corporate_filings: one row per filing a company made (current reports,
-- quarterly and annual reports, ownership reports). form is the form type
-- as filed (8-K, 10-Q, 4, 13F-HR, ...). items holds the current report
-- item codes, comma separated ('2.02,9.01'), NULL when the form has none.
-- known_at is the acceptance time (naive UTC): nothing may use a filing
-- before it.
CREATE TABLE corporate_filings (
    accession_number VARCHAR   PRIMARY KEY,
    ticker           VARCHAR   NOT NULL,
    issuer_cik       VARCHAR   NOT NULL,
    form             VARCHAR   NOT NULL,
    filing_date      DATE      NOT NULL,
    known_at         TIMESTAMP NOT NULL,
    period_of_report DATE,
    items            VARCHAR,
    url              VARCHAR,
    source           VARCHAR   NOT NULL,
    updated_at       TIMESTAMP NOT NULL
);
CREATE INDEX idx_corporate_filings_ticker ON corporate_filings (ticker, known_at);

-- institutional_holdings: the positions an institutional manager reports
-- each quarter (a 13F holdings report), one row per line of its holdings
-- table. Keyed by the report and the line, so a re-ingest is idempotent.
-- Securities are named by CUSIP; ticker is filled when the lake knows the
-- CUSIP (instruments.cusip). value_usd is in dollars. amount_type is
-- 'shares' or 'principal'; put_call 'put', 'call' or NULL;
-- investment_discretion 'sole', 'defined' or 'other'.
CREATE TABLE institutional_holdings (
    accession_number      VARCHAR   NOT NULL,
    line                  INTEGER   NOT NULL,
    filer_cik             VARCHAR   NOT NULL,
    filer_name            VARCHAR,
    report_period         DATE      NOT NULL,
    filing_date           DATE      NOT NULL,
    known_at              TIMESTAMP NOT NULL,
    cusip                 VARCHAR   NOT NULL,
    issuer_name           VARCHAR,
    security_class        VARCHAR,
    ticker                VARCHAR,
    amount                DOUBLE,
    amount_type           VARCHAR,
    value_usd             DOUBLE,
    put_call              VARCHAR,
    investment_discretion VARCHAR,
    source                VARCHAR   NOT NULL,
    PRIMARY KEY (accession_number, line)
);
CREATE INDEX idx_institutional_holdings_cusip ON institutional_holdings (cusip, report_period);
