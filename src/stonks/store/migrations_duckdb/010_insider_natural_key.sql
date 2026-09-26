-- NULL-safe natural key for insider_transactions.
--
-- Migration 005 deduplicated on a unique index over
-- (ticker, transaction_date, owner_name, transaction_code, shares, sec_link).
-- DuckDB unique indexes treat NULLs as distinct, so any row with a NULL in
-- one of those columns (STOCK-Act disclosures with NULL shares, rows
-- without a sec_link, ...) never conflicted and was re-inserted on every
-- ingest. DuckDB supports neither expression indexes nor generated columns
-- as ON CONFLICT targets, so the key is materialized as a plain column.
--
-- natural_key = md5 over the six key parts, each rendered as 'v' || value
-- (or 'n' for NULL) so NULL and '' stay distinct, joined by chr(31).
-- transaction_date is normalized to DATE and shares to DOUBLE so 100 and
-- 100.0 hash alike. This expression MUST stay identical to
-- ``_INSIDER_NATURAL_KEY_SQL`` in stonks/store/lake.py, which computes the
-- key for new rows at upsert time.
--
-- DuckDB can't CREATE INDEX in a transaction that has pending UPDATEs /
-- DELETEs, so instead of altering in place we copy the de-duplicated rows
-- aside, recreate the table with the key as a UNIQUE constraint, and copy
-- them back. Every row survives except duplicates, where the most recently
-- inserted copy (highest id) wins — the outcome an upsert would have had.
-- The DROP TABLE below is allowlisted in lake._DATA_PRESERVING_DROPS.

CREATE TEMP TABLE _insider_transactions_010 AS
SELECT *,
       md5(concat_ws(chr(31),
           COALESCE('v' || ticker, 'n'),
           COALESCE('v' || CAST(CAST(transaction_date AS DATE) AS VARCHAR), 'n'),
           COALESCE('v' || owner_name, 'n'),
           COALESCE('v' || transaction_code, 'n'),
           COALESCE('v' || CAST(CAST(shares AS DOUBLE) AS VARCHAR), 'n'),
           COALESCE('v' || sec_link, 'n')
       )) AS natural_key
  FROM insider_transactions
QUALIFY row_number() OVER (PARTITION BY natural_key ORDER BY id DESC) = 1;

DROP INDEX IF EXISTS uq_insider_natural;
DROP TABLE insider_transactions;

CREATE TABLE insider_transactions (
    id                      INTEGER PRIMARY KEY DEFAULT nextval('insider_transactions_id_seq'),
    ticker                  VARCHAR NOT NULL,
    transaction_date        DATE    NOT NULL,
    filing_date             DATE,
    owner_name              VARCHAR,
    owner_cik               VARCHAR,
    owner_relation          VARCHAR,
    owner_title             VARCHAR,
    transaction_code        VARCHAR,
    acquired_disposed       VARCHAR,    -- 'A' or 'D' (SEC Form 4 standard)
    shares                  DOUBLE,
    price                   DOUBLE,
    value                   DOUBLE,
    post_transaction_amount DOUBLE,
    sec_link                VARCHAR,
    natural_key             VARCHAR NOT NULL UNIQUE
);

INSERT INTO insider_transactions
SELECT id, ticker, transaction_date, filing_date, owner_name, owner_cik,
       owner_relation, owner_title, transaction_code, acquired_disposed,
       shares, price, value, post_transaction_amount, sec_link, natural_key
  FROM _insider_transactions_010;

DROP TABLE _insider_transactions_010;
