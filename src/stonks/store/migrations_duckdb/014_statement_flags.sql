-- Statement audit (BL-36).
--
-- statement_flags: one row per financial-statement period that failed an
-- accounting check in stonks.store.audit (balance-sheet identity, net
-- income across statements, cash, gross profit, quarters vs annual,
-- filing date before period end, negative shares). The audit replaces the
-- flags of the tickers it audits, so re-running it is idempotent and a
-- fixed row loses its flag. The statement rows themselves are never
-- changed. Readers opt in to skipping flagged periods.
--
-- severity is 'error' (the row cannot be right) or 'warning' (the row is
-- suspect). check_id names the check (CHECK is a reserved word).
CREATE TABLE statement_flags (
    ticker      VARCHAR   NOT NULL,
    period_end  DATE      NOT NULL,
    frequency   VARCHAR   NOT NULL,
    check_id    VARCHAR   NOT NULL,
    severity    VARCHAR   NOT NULL CHECK (severity IN ('error', 'warning')),
    detail      VARCHAR,
    flagged_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, period_end, frequency, check_id)
);
