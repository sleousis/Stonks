-- Short selling (roadmap 16.1): whether an order opens or closes a position.
--
--   position_effect  'open' or 'close'. A sell that opens is a short sale,
--                    a buy that closes is a cover. NULL for long-only
--                    orders and every row written before this migration,
--                    which read as today: buys open, sells close.
--
-- side keeps its CHECK (side IN ('buy', 'sell')).

ALTER TABLE orders ADD COLUMN position_effect TEXT
    CHECK (position_effect IS NULL OR position_effect IN ('open', 'close'));
