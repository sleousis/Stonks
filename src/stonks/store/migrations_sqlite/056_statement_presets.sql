-- Broker export presets for CSV statements (DEGIRO first).
--
-- A statement import can come from a ready preset (a broker export Stonks
-- knows, such as the DEGIRO Transactions file) instead of a column
-- mapping. It records which preset read it. A holdings export (such as the
-- DEGIRO Portfolio file) adds no activities: it sets the holdings and cash
-- of the portfolio on its day, kept in statement_import_holdings. Undo
-- deletes those rows like it deletes an import's activities.

ALTER TABLE statement_imports ADD COLUMN preset TEXT;
ALTER TABLE statement_imports ADD COLUMN kind TEXT NOT NULL DEFAULT 'activities'
    CHECK (kind IN ('activities', 'holdings'));
-- The day a holdings export describes (ISO date). NULL for activities.
ALTER TABLE statement_imports ADD COLUMN as_of TEXT;

CREATE TABLE IF NOT EXISTS statement_import_holdings (
    import_id    TEXT NOT NULL REFERENCES statement_imports(id) ON DELETE CASCADE,
    row_id       TEXT NOT NULL,                 -- stable per line: duplicate protection
    raw_symbol   TEXT NOT NULL,                 -- ISIN, broker symbol, or CASH:<ccy>
    ticker       TEXT,                          -- NULL = not covered, or a cash line
    quantity     REAL NOT NULL,
    price        REAL,
    market_value REAL,                          -- in the line's currency
    currency     TEXT,
    description  TEXT,
    is_cash      INTEGER NOT NULL DEFAULT 0 CHECK (is_cash IN (0, 1)),
    PRIMARY KEY (import_id, row_id)
);
