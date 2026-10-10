-- Each notification delivery records which worker claimed it.
--
-- A worker claims a batch of rows with a lease and sends them one by one.
-- When a slow channel makes the batch outlast the lease, another worker
-- may claim the rows still waiting. claimed_by lets the first worker see
-- that and skip them, so a row is never sent by two workers. NULL means no
-- worker holds it.

ALTER TABLE notification_deliveries ADD COLUMN claimed_by TEXT;
