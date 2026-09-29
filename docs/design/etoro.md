# eToro

This page says what eToro's official public API allows, how its ideas map to Stonks, what Stonks builds on it and what it leaves out. Everything here comes from eToro's own documentation and terms, read on 29 September 2026. The API is a beta product and eToro changes it often, so check the [changelog](https://api-portal.etoro.com/core/changelog) before relying on a detail.

Stonks uses only the official public API at `https://public-api.etoro.com`. It never scrapes the eToro site or calls an unofficial endpoint.

## What the API allows

### Keys and scopes

- Each request carries two keys: `x-api-key` (the public API key) and `x-user-key` (the user key), plus a fresh `x-request-id` UUID. Source: [Authentication](https://api-portal.etoro.com/core/getting-started/authentication).
- You make keys in the eToro app under **Settings > Trading > API Key Management > Create New Key**. Your account must be verified first. Source: [Introduction](https://api-portal.etoro.com/) and [Authentication](https://api-portal.etoro.com/core/getting-started/authentication).
- Each key is for one **environment**: Demo or Real. You need two keys to use both.
- Each key has a **permission**: Read (portfolio data) or Write (trades).
- A key can carry an IP allow list and an expiry date. Creating one needs a code sent to your phone.
- OAuth 2.0 is the other way in, meant for apps that sign other eToro users in. Its scopes include `etoro-public:real:read`, `etoro-public:real:write`, `etoro-public:demo:read` and `etoro-public:demo:write`. The key pair has the same permissions as the scopes. Source: [OpenAPI reference](https://api-portal.etoro.com/api-reference/openapi.json).
- eToro advises long-lived keys be kept out of source code and rotated about every 90 days. Source: [Authentication deep dive](https://builders.etoro.com/learn/authentication-and-api-keys).

### Demo and real accounts

Most trading endpoints come in a real and a demo form (`/trading/info/real/pnl` and `/trading/info/demo/pnl`, `/trading/execution/orders` and `/trading/execution/demo/orders`). The demo account trades virtual money. Source: [Open and close market orders](https://api-portal.etoro.com/core/guides/market-orders).

### Endpoints Stonks uses

| Need | Endpoint | Source |
|---|---|---|
| Account ids | `GET /api/v1/me` (returns `realCid`, `demoCid`) | [Get authenticated user profile](https://api-portal.etoro.com/api-reference/identity/get-authenticated-user-profile) |
| Positions, cash, pending orders | `GET /api/v1/trading/info/real/pnl` and `.../demo/pnl` | [Get account PnL and portfolio details](https://api-portal.etoro.com/api-reference/trading--real/get-account-pnl-and-portfolio-details) |
| Equity | Available cash plus total invested plus unrealized P&L, from the same answer | [Calculate Equity](https://api-portal.etoro.com/core/guides/calculate-equity) |
| Closed trades | `GET /api/v1/trading/info/trade/history` and `.../trade/demo/history` | [List trading history](https://api-portal.etoro.com/api-reference/trading--real/list-trading-history) |
| Instruments | `GET /api/v2/market-data/instruments` by ids or symbols, `GET /api/v1/market-data/exchanges` | [Search for securities](https://api-portal.etoro.com/api-reference/market-data/search-for-securities-instruments-by-id-or-symbol), [Find Instrument ID](https://api-portal.etoro.com/core/guides/get-instrument-id) |
| Prices | `GET /api/v2/market-data/rates` (bid and ask) | [Retrieve bid/ask rates](https://api-portal.etoro.com/api-reference/market-data/retrieve-bidask-rates-for-one-or-more-instruments) |
| Can this be traded, and how | `POST /api/v2/trading/info/eligibility` | [Check instrument trading eligibility](https://api-portal.etoro.com/api-reference/trading--real/check-instrument-trading-eligibility) |
| Open a position | `POST /api/v2/trading/execution/orders` and `.../demo/orders` | [Create an order](https://api-portal.etoro.com/api-reference/trading--real/create-an-order) |
| Order outcome | `GET /api/v2/trading/info/orders:lookup` by `orderId` or `referenceId` | [Get order information](https://api-portal.etoro.com/api-reference/trading--real/get-order-information-and-position-details) |
| Close a position | `POST /api/v1/trading/execution/market-close-orders/positions/{positionId}` | [Close position by units](https://api-portal.etoro.com/api-reference/trading--real/close-position-by-units) |
| Close outcome | `GET /api/v1/trading/info/real/close-orders/{orderId}` | [Get close order information](https://api-portal.etoro.com/api-reference/trading--real/get-close-order-information-and-closed-position-details) |
| Cancel | `DELETE /api/v2/trading/execution/orders/{orderId}`, `DELETE /api/v1/trading/execution/market-close-orders/{orderId}` | [Cancel an order](https://api-portal.etoro.com/api-reference/trading--real/cancels-an-order-before-it-is-executed), [Cancel pending close order](https://api-portal.etoro.com/api-reference/trading--real/cancel-pending-close-order) |

### Orders

- An order **opens** a position. Only `buy` and `sellShort` are accepted today. The `sell` and `close` actions are not available yet on the order endpoint. Source: [Create an order](https://api-portal.etoro.com/api-reference/trading--real/create-an-order).
- An order is sized by exactly one of `amount` (cash, USD only), `units` or `contracts` (futures).
- Order types are `mkt` (market), `mit` (market if touched) and `limitIOC` (a limit that executes now or is cancelled).
- `leverage` defaults to 1. More than 1 needs a stop loss.
- `settlementType` is `real` (the asset itself), `cfd`, `realFutures` or `marginTrade`. Which ones an instrument allows, for each direction and leverage, comes from the eligibility endpoint (`leverageConfigs`).
- The eligibility answer also says whether units may be fractional (`unitsQuantityType`), whether an order may be sized in units, amount or both (`allowedOrderQuantityType`), the minimum position size and the most units per order.
- The `x-request-id` header makes a create idempotent. The order's `referenceId` echoes it, and the lookup endpoint finds an order by it. A 200 answer means accepted, not filled.
- The lookup statuses are 1 Received, 2 Placed, 3 Filled, 4 Rejected, 5 PartiallyFilled, 6 PendingCancel, 7 Canceled, 8 Expired, 9 CanceledPartiallyFilled, 10 RejectedPartiallyFilled, 11 WaitingForMarket and 12 PendingTriggeredRate. 3 and 5 mean the order executed.
- A position is **closed** by its `positionId`, in full or in part (`UnitsToDeduct`). You cannot simply sell an instrument. Source: [Open and close market orders](https://api-portal.etoro.com/core/guides/market-orders).
- Stop loss and take profit are rates on an open position, and can be changed with `PATCH /api/v2/trading/positions/{positionId}`.
- Shorting is `sellShort`, which needs a stop loss and is not a real asset.

### Rate limits

- Limits count per user key over a rolling minute. Past the limit the API answers `429`, and eToro asks for retries with exponential backoff. Source: [Rate Limits](https://api-portal.etoro.com/core/getting-started/rate-limits).
- Most reads share one budget of 60 requests a minute. Market data allows 120.
- Opening, closing and cancelling share one budget of 20 requests a minute.
- Answers carry `RateLimit-Limit` and `RateLimit-Remaining` headers.

### The terms

The API is governed by the [eToro Builders' Economy terms](https://www.etoro.com/wp-content/uploads/2026/03/Master_eToro_Builders_Economy_Terms_17-Feb-2026-clean_R.pdf) (17 February 2026). The parts that shape Stonks:

- **Permitted use** is managing your own eToro account and building tools for your own personal use. Use on behalf of third parties is excluded (Part I 1.2, Part V 1.5).
- Automated tools are allowed, at your own risk. You must review and test any tool before relying on it, and monitor your account.
- Keys are confidential and may not be shared, sold or transferred. A compromise must be reported to eToro within 24 hours, and the keys rotated (Part II 1).
- You must keep to every rate limit and throttle, and must not send excessive or duplicate requests or bulk-cache data beyond what the permitted use needs (Part II 2.1 to 2.4).
- A tool that trades on its own must have its own rate limiting, a cap on how often it orders, and back-off logic (Part II 2.5).
- Orders stay subject to the trading account's own rules, and the API must not be used to get around suitability or appropriateness checks (Part II 2.7 and 2.8).
- Data from the API ("Licensed Content", such as charts, prices and news) is for personal use only. It may not be redistributed, used to train models, or stored in separate databases (Part II 3, Part V 1.7 and 1.8).
- Wash trading, simultaneous opposing positions, scalping and latency gaming are forbidden (Part V 1.2).
- On a defect or security issue you must stop connecting until eToro allows it again (Part II 2.6).

### SnapTrade

SnapTrade lists eToro for the US and Europe, read only, through eToro's own sign-in. Trading is "not yet" supported and past transactions are not available. Source: [SnapTrade eToro integration](https://snaptrade.com/brokerage-integrations/etoro-api). An admin who already runs SnapTrade can link an eToro account through it with no eToro keys at all, for positions and cash only.

## How eToro maps to Stonks

| eToro | Stonks |
|---|---|
| The demo account | A broker portfolio at the **Broker paper** stage. Its trader says it trades no real money. |
| The real account | A broker portfolio. Opening orders need **Real money, small** or higher. Closing orders go out at any stage. |
| A position (one line per purchase) | Positions of the same instrument are added up into one holding. The trader closes the oldest positions first. |
| Units | Quantity. Stonks sends orders in units, so no conversion is needed. |
| Amount | Used only when an instrument accepts amounts and not units. Stonks converts its quantity with eToro's ask price for that instrument, for USD instruments only, and refuses the order otherwise. |
| `instrumentId` | A ticker such as `AAPL.US`, through the instrument's symbol, type and exchange. Mappings are kept in memory for the process, as eToro advises, since ids never change. An admin can pin a mapping under `[connections.etoro].instrument_overrides`. |
| Stocks and ETFs | `AAPL.US`, `BARC.LSE`, `SAP.XETRA` and so on, for the exchanges Stonks already maps. Others are "not covered" and kept with their eToro symbol. |
| Crypto | `BTC-USD.CC`. |
| Currencies, indices, commodities | Not covered. These trade only as CFDs at eToro. |
| `settlementType` real | The only kind the trader opens. |
| CFDs, leverage, margin trades, futures | Synced and shown as holdings. The trader never opens them. |
| Copied traders (mirrors) | Their positions are synced as holdings. The trader never touches them. |
| `x-request-id` | The order's client id, turned into a fixed UUID, so a repeat is a no-op and the order can be found again by its client id. |
| Order statuses | The order state machine: Received is `submitted`, Placed, WaitingForMarket and PendingTriggeredRate are `accepted`, Filled is `filled`, Rejected is `rejected`, PendingCancel is `pending_cancel`, Canceled is `cancelled`, Expired is `expired`. PartiallyFilled, CanceledPartiallyFilled and RejectedPartiallyFilled are `cancelled` with the filled part booked, since the rest will not fill. |

## What Stonks supports

- **Reads.** A connection reads the account id, cash, equity and positions, and closed trades as activities. Each open position becomes a buy activity, and each closed trade a buy and a sell, so Insights, the behaviour report and tax lots work.
- **Trading, off by default.** With `[connections.etoro] trading = true`, a linked portfolio's auto and approve books, manual orders and approved tickets trade through the connection. The trader:
  - takes market orders only
  - opens long positions only, with leverage 1 and settlement `real`
  - refuses an instrument that trades only as a CFD, a fractional quantity where only whole units are allowed, and a size above eToro's maximum per order
  - turns a sell into closes of the oldest matching real positions, and refuses a sell of more than it holds, so it never opens a short
  - reports order states by client id, cancels working orders for the kill switch, and lists working orders for reconciliation.
- A **real** account also needs `[connections.etoro] allow_real_money = true`. Even then, opening orders wait for the Real money stages.
- **Safeguards from the terms.** Every call goes through a read budget of 50 a minute per connection and an order budget of at most 10 a minute per connection (both below eToro's limits). A `429` is retried with exponential backoff, honouring the retry time eToro sends, and then the call gives up. The keys are sealed per connection, never logged, and scrubbed from every error.
- Every order passes the usual path: the order state machine, the stage guard, the risk rules, halts and the kill switch.

## Gaps

- **No limit, stop or bracket orders.** eToro has no resting limit or stop order in the public API, only `mit` and an immediate `limitIOC`. Stonks sends market orders only. Protective stops at the broker are not built, although eToro can hold a stop loss on a position.
- **No shorts, leverage or CFDs.** eToro's shorts are CFDs that need a stop loss. The terms and Stonks' own rules make these a poor fit, so the trader refuses them.
- **No dividends, fees or cash moves as activities.** The API gives no dividend or deposit list for the trading account. Dividends and overnight fees appear only as one total per position (`totalFees`). The cash account transaction list is for eToro Money cards and transfers, not the trading account.
- **Closing orders are not tagged.** Closes have no reference Stonks can search by later. The trader records their eToro order ids on the Stonks order, so reconciliation finds them. If Stonks stops between sending a close and recording it, the end-of-day drift check catches the difference.
- **No what-if preview in Stonks.** eToro has a cost preview (`/api/v2/trading/info/costs`), but it is not wired into the live preview yet.
- **Prices are USD only for amount orders.** Amount sizing works for USD instruments only.
- **One user's keys, one person.** The terms allow use for your own account only. Each person connects their own eToro keys. Stonks never uses one person's eToro keys for another.
- **Daily books, not scalping.** The terms forbid scalping and latency games. Stonks keeps to one order budget of 10 a minute per connection and never opens opposing positions, but it does not stop an intraday book from using an eToro portfolio. Use eToro for daily books.
- **eToro data stays out of the lake.** Prices and candles from eToro are Licensed Content. Stonks reads a price only to size an order and never stores eToro market data or uses it for research.

## How to connect

See [Operations: eToro](../operations.md#etoro) for the key settings, the console and CLI steps, and what to do when a key expires or leaks.
