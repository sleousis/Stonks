# Review of the roadmap merge, 2026-09-27

This page lists what a review of `ea6c0dd7..c2044efb` (Python under `src`) found. The range covers Phase 19 wave 1, the IBKR adapter, tickets and the submit window, the Phase 20 backend, calendars, the screener and the Phase 22 research work.

Each fixed finding has a failing test first, then the fix, in its own commit. Line numbers point at `c2044efb`.

## Summary

| Severity | Found | Fixed |
| --- | --- | --- |
| Critical | 1 | 1 |
| High | 7 | 6 |
| Medium | 13 | 12 |
| Low | 11 | 3 |

## Merge integrity

The conflict resolutions look clean. No router, MCP tool, CLI command, hook or rule is registered twice. All 14 scheduler actions exist on all three backends. No pydantic field or config key is defined twice. Migrations 027 to 033 apply on a 026 install with data, and the rebuilt tables keep their rows and foreign keys. The new upserts are idempotent.

The real gaps are code written before 19.8 that never learned about the `approve` mode or the live context (see below).

## Findings

### Critical

- `src/stonks/app/manual_orders.py:303` Manual orders on `pf_default` at a live IB Gateway counted as paper, so they skipped step-up and the typed confirmation. Fixed: the check now uses `broker_mode`.

### High

- `src/stonks/production/manual.py:252` Manual orders and approved drafts at a real broker had no live context, so the notional caps, price band, capital ramp, protections and account rules did nothing. Fixed.
- `src/stonks/production/auto_pause.py:70` and five more places. Pauses only matched `mode = 'auto'`, so broker errors, gateway outages, sync failures, demotions and disabled users never paused an `approve` subscription. Re-enabling one skipped step-up too. Fixed.
- `src/stonks/tax/lots.py:175` Tax lots ignored splits, so a post-split sale showed a wrong loss and a phantom short. Fixed: the gains export reads splits from the corporate action ledger.
- `src/stonks/tax/lots.py:257` A lot already sold counted as a wash sale replacement, and the disallowed loss was lost for good. Fixed.
- `src/stonks/screener/data.py:118` The screener read current statement rows, so a restatement leaked back into past dates (P12). This also fed rule universes and so backtests. Fixed: it reads the version known on the date.
- `src/stonks/assistant/catalog.py:167` The assistant could create and run a code draft (Python in the API server) with no confirmation. Fixed: code drafts wait for the person.
- `src/stonks/execution/brokers/__init__.py:101` Every IBKR broker from `make_broker` uses the tick's client id. While a tick holds the gateway, the API kill switch and manual orders cannot connect. Not fixed: it needs a design choice (a separate client id plus a master client id or `reqAllOpenOrders`, so the API still sees the tick's orders).

### Medium

- `src/stonks/production/manual.py:411` Manual orders skipped the reconciliation gate and left a submit with no answer `pending` instead of `unknown`. Fixed.
- `src/stonks/production/manual.py:507` A change placed the replacement when the cancel was only requested, so both could fill. Fixed: it waits for a confirmed cancel.
- `src/stonks/production/manual.py:205` Two manual orders at once on a simulated book could lose one ledger update, and the same key twice could book two fills. Fixed: the book is rechecked under the write lock.
- `src/stonks/production/submit.py:151` The submit job sent approved tickets for books paused, retired, archived or disabled after the tick. Fixed.
- `src/stonks/production/submit.py:148` A pending order whose lookup failed at startup made its ticket `submitted` though nothing was sent. Fixed: the window waits for a clean startup reconcile.
- `src/stonks/lab/survival/deflated_sharpe.py:152` Deflated Sharpe took the larger of the class and family trial counts instead of both together (P2). Fixed.
- `src/stonks/screener/data.py:119` The screener used a statement on its filing day and a 45 day stand-in for a missing filing date, where the lake uses the next day and 90 days. Fixed.
- `src/stonks/api/routers/telegram.py:141` An API token could mint a Telegram link code, and the linked chat then acts with the person's whole role. Fixed: browser session only. A link still outlives a password or 2FA reset (not fixed).
- `src/stonks/assistant/catalog.py:179` The assistant could cancel anyone's queued job, change other people's drafts (as an admin) and switch price alerts off without asking. Fixed.
- `src/stonks/accounts/rules/inputs.py:82` Day trades were counted once per ticker and day, missed shorts, and counted selling an old holding then buying back. Fixed.
- `src/stonks/app/tax.py:237` Commissions in another currency went into the cost as if in the trade currency. Fixed for the gains export. The settlement ledger (`accounts/rules/settlement.py:53`) has the same gap (not fixed).
- `src/stonks/price_alerts/evaluate.py:211` Price alerts compared raw closes, so a split fired false crossings. Fixed.
- `src/stonks/insights/flows.py:174` Broker deposits and withdrawals are summed without their currency. Not fixed: it only matters when an activity's currency differs from the account's, and the fix needs the account currency on the flow.

### Low

- `src/stonks/assistant/loop.py:525` A tool result could close its untrusted block early. Fixed.
- `src/stonks/telegram/channel.py:70` Any HTTP 400 unlinked the person's chat. Fixed: only a lost chat does.
- `src/stonks/app/cash_flows.py:105` Recording a cash flow could race a tick or a manual order. Fixed.
- `src/stonks/production/submit.py:191` A cancelled order can be sent again under the same client id after a resume. Not fixed.
- `src/stonks/assistant/loop.py:329` Two turns at once can pass the write rate limit together. Not fixed.
- `src/stonks/lifecycle/retrain.py:345` A version records `train_end` one day later than the fit used, and a retrain drops a stored universe for the manifest's frozen tickers. Not fixed.
- `src/stonks/strategies/examples/tsmom.py:153` The fast path finds month ends from the next bar. It only feeds pruning and heatmap scores. Not fixed.
- `src/stonks/app/portfolio.py:305` The trading mode view shows `pf_default` at a live IB Gateway as paper. Not fixed: the fix changes the API schema.
- `src/stonks/store/migrations_sqlite/027_complete_product.sql:143` Jurisdiction, wash sales and base currency live in both `portfolio_tax_settings` and `account_profiles`, with nothing keeping them in step. Not fixed.
- `src/stonks/store/migrations_sqlite/028_live_broker.sql` Nothing writes `account_restricted` or `product_documents`, so the KID rule can never let a buy through. Not fixed.
- `src/stonks/auth/policy.py:74` Viewers cannot clear their own assistant freeze or link Telegram. Not fixed: a policy call.
