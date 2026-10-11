-- Why a lab run failed, kept in the trial ledger.
--
-- `error` is a plain sentence everyone who may read the ledger sees. The
-- exception text ("TypeError: cannot pickle ...", secrets scrubbed) goes in
-- `error_detail`, which the API returns to admins only, like
-- `jobs.error_detail`. Both NULL for runs that finished with a verdict.

ALTER TABLE lab_runs ADD COLUMN error TEXT;
ALTER TABLE lab_runs ADD COLUMN error_detail TEXT;
