-- The default book follows every active strategy (roadmap 15.5 follow-up).
--
-- The tick now builds its books from subscriptions by default. The old
-- single book traded pf_default over every active strategy, so each active
-- strategy that pf_default has no subscription to gets one here: weight 1,
-- paper, or auto when pf_default is a broker portfolio. From now on
-- stonks.accounts.default_book does the same whenever a strategy turns
-- active. An existing subscription (any mode, enabled or not) is kept.
-- Each new row is audited as service:system.

CREATE TEMP TABLE _default_book_new AS
SELECT 'sub_' || lower(hex(randomblob(6))) AS id, p.owner_id AS user_id,
       s.id AS strategy_id, p.id AS portfolio_id,
       CASE WHEN p.kind = 'broker' THEN 'auto' ELSE 'paper' END AS mode,
       strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') AS created_at
  FROM strategies s
  JOIN portfolios p ON p.id = 'pf_default' AND p.status = 'active'
 WHERE s.status = 'active'
   AND NOT EXISTS (
       SELECT 1 FROM subscriptions x
        WHERE x.portfolio_id = 'pf_default' AND x.strategy_id = s.id
   );

INSERT INTO subscriptions
    (id, user_id, strategy_id, portfolio_id, mode, weight, risk_overrides_json,
     created_at, updated_at)
SELECT id, user_id, strategy_id, portfolio_id, mode, 1.0, '{}', created_at, created_at
  FROM _default_book_new;

INSERT INTO audit_log (actor, action, target_kind, target_id, portfolio_id, details_json, created_at)
SELECT 'service:system', 'subscription.create', 'subscription', id, portfolio_id,
       json_object('mode', mode, 'reason', 'default_book', 'strategy_id', strategy_id),
       created_at
  FROM _default_book_new;

DROP TABLE _default_book_new;
