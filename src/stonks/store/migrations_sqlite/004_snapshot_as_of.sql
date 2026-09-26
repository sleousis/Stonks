-- The trading date a real portfolio snapshot was taken for (the tick's
-- ``as_of``), separate from the wall-clock ``taken_at``. P&L groups by it and
-- the tick refuses to run for a date earlier than the latest one. Existing
-- rows are backfilled from the UTC date of ``taken_at``.
ALTER TABLE portfolio_snapshots ADD COLUMN as_of TEXT;

UPDATE portfolio_snapshots
   SET as_of = substr(taken_at, 1, 10)
 WHERE as_of IS NULL
   AND taken_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*';

CREATE INDEX IF NOT EXISTS idx_snapshots_as_of ON portfolio_snapshots(as_of);
