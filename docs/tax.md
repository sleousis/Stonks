# Currency and tax

Each portfolio has a base currency. Values, P&L and tax files are shown in it. Roadmap 20.5. Code: `src/stonks/fx/`, `src/stonks/tax/`, `src/stonks/app/tax.py`.

This is not tax advice. The files help you or your accountant file. Check them before you use them.

## Base currency

- Set it when you create a portfolio, or later with `PUT /api/tax/settings` or `stonks tax settings --base-currency EUR`.
- Cash is taken as already in the base currency.
- An instrument with no currency in the lake is taken to trade in the base currency.

## One home per setting

The tax settings hold the jurisdiction and whether wash sales apply. The portfolio holds the base currency. A live portfolio's account profile reads all three from there, and saving a profile writes the jurisdiction and base currency there. The profile's own `wash_sale_mode` only says what the pre-trade guard does when wash sales apply: `warn` or `block`. Turn wash sales off here and the guard is off too. While a portfolio trades real money (`live_small` or `live_scale`), the jurisdiction and base currency are locked here like the profile.

## FX rates

Rates live in the lake table `fx_rates`. One row per pair and day: how many units of the quote currency one unit of the base currency buys at the close.

```bash
uv run stonks ingest fx --pairs EURUSD,GBPUSD --since 2024-01-01
```

EODHD serves them (`EURUSD.FOREX`). To convert on a day, Stonks takes the latest rate on or before that day. It uses the inverse pair when only that one is stored, and a cross through USD when needed. London prices in pence (`GBX`) count as GBP / 100. Orders, stops and the portfolio view show London prices in pounds, and a manual limit for a London stock is in pounds too.

A missing rate is never guessed. The amount stays unconverted, the base total is empty, and `fx_missing` names the currency. `GET /api/fx/rate?base=EUR&quote=USD` shows the rate Stonks would use.

## What is converted

| Where | Field | How |
|---|---|---|
| `GET /api/portfolio` | `market_value_base`, `positions_value_base`, `total_value_base` | each holding at the rate of its price day |
| `GET /api/pnl` | `base_rows` | each day's holdings at that day's close and rate, cash as is |
| `GET /api/insights` | `total_value_base` | holdings at the latest rate |
| `GET /api/tca/summary` | `groups_base` | each order at the rate of its decision day |

Existing fields keep their meaning: they are not converted.

## Realized gains

`GET /api/tax/exports/gains?year=2025` or `stonks tax gains --year 2025`. One row per lot sold in the year.

- **FIFO** by default: a sale closes the oldest lots first.
- **Specific lots**: set `lot_method` to `specific`, then pick the lots each sale closes with `PUT /api/tax/lots/picks`. What you did not pick is closed FIFO.
- Fees are in the cost of a buy and come off the proceeds of a sale.
- **Holding period**: long when held more than one year, else short.
- **Short sales**: a sale beyond what you hold opens a short. A later buy covers it. The gain is realized at the cover and is always short term.
- **Base amounts**: the cost at the purchase day's rate, the proceeds at the sale day's rate.

## Wash sales (US)

With jurisdiction `us` and `wash_sales` on (the default), a loss is disallowed when you buy the same ticker within 30 days before or after the sale. Only the part covered by the replacement shares is disallowed. That amount moves into the replacement's cost, so you get it back when you sell those shares. Each replacement share is used once.

Not covered: covers of short sales, and carrying over the holding period of the sold lot.

## EU and UK

No wash sale adjustment. UK share matching rules (same day, 30 day, section 104 pool) are not built. FIFO is used, so check UK files with care.

## Dividends

`GET /api/tax/exports/dividends?year=2025` or `stonks tax dividends --year 2025`. From the corporate action ledger: gross is the amount per share times the shares held, net is the cash credited, and withholding is the difference.

Deposits and withdrawals are not income. They are recorded as cash flows and taken out of the returns (see "Returns and cash flows" in `operations.md`).

## CSV columns

Gains: `ticker, lot_kind, quantity, acquired, disposed, holding_period, currency, proceeds, cost_basis, wash_sale_disallowed, gain, base_currency, proceeds_base, cost_basis_base, wash_sale_disallowed_base, gain_base, open_fill_id, close_fill_id`.

Dividends: `ticker, ex_date, quantity, per_share, currency, gross, withholding, net, base_currency, gross_base, withholding_base, net_base`.

## Who can do what

Reads need your own portfolio (another person's is a 404). Changing settings and lot picks needs `portfolio.manage` and writes an audit row.
