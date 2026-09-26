# TVL deviation strategy (`tvl_deviation`)

`TVLDeviationStrategy` (`strategies/examples/tvl_deviation.py`) trades one
crypto ticker (default `ETH-USD.CC`) on daily bars. It compares the price
with the price implied by a chain's DeFi total value locked (TVL, default
`ethereum`) and buys when the price is well below that level.

- **Source:** [neurotrader888/TVLIndicator](https://github.com/neurotrader888/TVLIndicator),
  `tvl_indicator.py`. MIT License. Stonks' own implementation, no code copied.
- **Data:** daily bars of `ticker` plus the `defi_tvl` lake table. Fill it
  with `stonks ingest tvl --chains ethereum [--since YYYY-MM-DD]`.
- `applicable_asset_classes = ("crypto",)`. The broker is long-only, so the
  strategy is long or flat.

## Data: `defi_tvl`

| column | meaning |
|---|---|
| `chain` | canonical lower-case chain name (`ethereum`, `solana`) |
| `observation_date` | UTC day the vendor stamps the value with (not the day it was published) |
| `tvl_usd` | total value locked, USD |
| `source` | the `DataSource.source_id` that wrote the row (provenance only) |

Primary key `(chain, observation_date)` (migration `011_defi_tvl.sql`).
Upserts are idempotent and the last write wins. DefiLlama is the first
source (`ingest/sources/defillama.py`, no API key,
`GET https://api.llama.fi/v2/historicalChainTvl/{chain}`, which excludes
liquid staking and double-counted TVL). Other vendors map onto the same
columns. Each chain is one unit in the `ingest_runs` row, so an unknown
chain (HTTP 404) or an outage fails only that chain. Only connection errors,
timeouts, HTTP 429 and 5xx are retried.

## Indicator

For each daily bar:

1. `tvl` = the latest TVL stamped on or before the day **before** the bar
   (see "Causality" below), carried forward over missing days.
2. OLS fit of `log(close) = a + b * log(tvl)` over the last `fit_length`
   bars, including the current bar.
3. `pred = exp(a + b * log(tvl_now))`.
4. `ind = (close - pred) / ATR`, where ATR is the simple rolling mean of true
   range over `atr_lookback` bars (`features.indicators.atr(method="sma")`).

If TVL is constant across the fit window, the slope is undefined. The fit
then falls back to the window mean of `log(close)`.

## Rule (added by Stonks)

The original only builds the indicator and prints next-day return tables. It
has no trading rule. Stonks adds a mean-reversion rule with hysteresis:

- go **long** when `ind < -threshold` (the price is far below its
  TVL-implied level);
- go **flat** when `ind > 0` (the price is back above that level);
- otherwise keep the current position. A NaN keeps it too.

The state is replayed over the last `3 * (fit_length + atr_lookback)` bars
on every call, so the result depends only on data up to `as_of`.
`estimate_return` is `-ind` while long (floored at `1e-6`) and `None`
otherwise. It is also `None` when TVL is missing, or when the value in use
is more than `max_tvl_age_days` old (default 3; a fresh value is 1 day old).
A stale value also counts as missing inside the fit window.

| parameter | default | bounds | tunable |
|---|---|---|---|
| `fit_length` | 7 | 5-60 | yes |
| `atr_lookback` | 30 | 10-90 | yes |
| `threshold` | 0.25 | 0-1 | yes |
| `chain` | `ethereum` | any | no |
| `max_tvl_age_days` | 3 | 1-30 | no |
| `interval` | `1d` | `1d` only | no |
| `ticker`, `allocation` | `ETH-USD.CC`, 1.0 | | no |

## Causality

DefiLlama stamps each point with 00:00 UTC of a day, and it keeps updating
the newest point during that day. The original shifts the TVL by one row,
so the value stamped `d` sits on the bar that closes at midnight `d`. That
treats the value as known at the same moment as that close.

Stonks is stricter. The value stamped `d` is used from the close of bar
`d + 1` onward: bar `b` only sees observations with
`observation_date <= b - 1 day`. `test_causal_tvl_stamped_on_the_bar_day_is_invisible`
rewrites every TVL value stamped on or after the bar's day and checks that
nothing at that bar changes.

## Why long below the line (sign check)

The original repository has no stored result tables. We re-ran its analysis
(Spearman correlation between `ind` and the next day's return, plus the mean
next-day return above and below each threshold). We used its bundled
`ETHUSDT86400.csv` (daily bars, 2018 to January 2023) with DefiLlama's
current `historicalChainTvl/ethereum` series, `fit_length=7` and
`atr_lookback=30`:

| alignment | Spearman (all) | mean next-day return, `ind < -0.25` | mean next-day return, `ind > 0.25` |
|---|---|---|---|
| original (value stamped `d` on the bar closing at midnight `d`) | -0.116 | +0.995% | -0.830% |
| value stamped `d` on the bar opening on `d` | -0.068 | +0.521% | +0.099% |
| Stonks (value stamped `d` on the bar opening on `d + 1`) | -0.054 | +0.304% | +0.004% |

The Spearman correlation is negative in every year from 2019 to 2022 under
every alignment (original: -0.106, -0.117, -0.286, -0.010). A price above
its TVL line tends to fall back, and a price below it tends to recover. So
the rule buys when `ind` is negative, not positive.

Most of the original's edge comes from its near-concurrent alignment. With
the causal lag the effect is much smaller (Spearman about -0.05; in 2019 the
`ind < -0.25` bucket was negative, -0.236%). Treat the defaults as a
starting point for the lab, not as a validated edge.
