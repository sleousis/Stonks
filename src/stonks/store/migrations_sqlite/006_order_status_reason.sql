-- Why an order ended up in its status when the broker's state alone doesn't
-- say (roadmap 8.5): a pre-trade rejection's message, or "not found at the
-- broker" for a row written 'pending' before a submission that never
-- arrived (the process died in between). NULL otherwise.
ALTER TABLE orders ADD COLUMN status_reason TEXT;
