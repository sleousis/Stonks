-- Versioned fundamentals (principle P12, roadmap 9.5.1).
--
-- A restatement overwrites a statement row in place. When the vendor keeps
-- the original filing_date, the restated numbers look known from the old
-- date: look-ahead for every fundamentals strategy.
--
-- Each statement table gets a <table>_versions twin: the same columns plus
-- known_at, the time Stonks first saw that version of the row (naive UTC).
-- Every upsert that changes a row appends the full merged row as a new
-- version. The current tables stay the latest view.
--
-- A decision reads, per (ticker, period_end, frequency), the latest version
-- whose known_at and filing date are both at or before it. The first
-- version of a period is the one as filed, so it counts from its filing
-- date whenever it was seen (history ingested late stays usable).
--
-- Existing rows are backfilled as their first version, stamped with their
-- filing date (the period end when there is none).
--
-- A later migration that adds a column to a statement table must add it to
-- its _versions twin too.

CREATE TABLE income_statement_versions AS
    SELECT *, CAST(COALESCE(filing_date, period_end) AS TIMESTAMP) AS known_at
      FROM income_statement;
ALTER TABLE income_statement_versions ALTER COLUMN known_at SET NOT NULL;

CREATE TABLE balance_sheet_versions AS
    SELECT *, CAST(COALESCE(filing_date, period_end) AS TIMESTAMP) AS known_at
      FROM balance_sheet;
ALTER TABLE balance_sheet_versions ALTER COLUMN known_at SET NOT NULL;

CREATE TABLE cash_flow_statement_versions AS
    SELECT *, CAST(COALESCE(filing_date, period_end) AS TIMESTAMP) AS known_at
      FROM cash_flow_statement;
ALTER TABLE cash_flow_statement_versions ALTER COLUMN known_at SET NOT NULL;
