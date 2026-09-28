-- Protective stops (roadmap 19.10, design docs/design/live-trading.md
-- section 4 "Broker-side protective stops").
--
-- orders.oca_group    the one-cancels-other group at the broker. A
--                     protective stop and the exits of its position share
--                     one, so a fill of one shrinks the others.
-- orders.protective   1 for a protective stop Stonks placed after an entry
--                     filled. It lives until cancelled (good till
--                     cancelled), is resized when the position changes and
--                     is cancelled when the position closes.

ALTER TABLE orders ADD COLUMN oca_group TEXT;
ALTER TABLE orders ADD COLUMN protective INTEGER NOT NULL DEFAULT 0
    CHECK (protective IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_orders_protective
    ON orders(portfolio_id, ticker) WHERE protective = 1;
