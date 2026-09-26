# Book strategies: Quantitative Momentum and Stocks on the Move

Two cross-sectional equity strategies from the book backlog
(`docs/research/book-lessons.md`, BL-38 and BL-39). Both rank a whole
universe, read split- and dividend-adjusted daily bars, are long-only, and
leave sizing to a `PortfolioConstructor`.

## Shared mechanics

- **Universe.** The `universe` param (comma-separated tickers) or, when it
  is empty, every ticker with daily bars in the lake whose asset class (when
  known) is `equity`. The cross section is computed once per day, on the
  first `estimate_return` call, and every ticker is answered from it, so the
  answer never depends on the order tickers are asked in.
  `score_universe(tickers, as_of, lake)` is the same selection over explicit
  tickers (the duck-typed hook from the backlog's seam list).
- **No look-ahead.** Only bars dated on or before `as_of` are read, through
  the look-ahead-safe `BarCache` on the adjusted basis. Calendar timing
  (the last session of a month, the Wednesday session of a week) comes from
  the US exchange holiday rules in `features/sessions.py`, never from the
  next bar.
- **Survivorship.** A ticker ranks only with the full lookback of history
  (a name listed mid-window waits) and a last bar no more than 10 calendar
  days old (a delisted or halted name drops out instead of ranking on frozen
  prices).
- **Orders.** Target weights go through `orders_from_targets`: sells first,
  and buys are scaled down so they never spend more than cash plus sale
  proceeds.
- Run both on daily bars with `rebalance_every_bars = 1`: they hold on days
  that aren't their trading day, and a coarser engine cadence could skip it.

## `quant_momentum` (Gray & Vogel, *Quantitative Momentum*)

`strategies/examples/quant_momentum.py`, features in `features/momentum.py`.

1. 12-2 momentum `r = C[t-21] / C[t-252] - 1` (skips the most recent month).
2. Keep the top `top_pct` of the universe by `r`.
3. Frog in the pan: among those, keep the `id_keep_pct` with the lowest
   information discreteness `ID = sign(r) * (%neg days - %pos days)` over the
   same window (Da, Gurun & Warachka).
4. Equal weight through the `equal_weight_top_n` constructor.
5. Rebalance on the last session of February, May, August and November;
   hold on every other day.

| param | default | notes |
|---|---|---|
| `formation_bars` | 252 | start of the window (`t-252`) |
| `skip_bars` | 21 | skipped recent bars; must be `< formation_bars` |
| `top_pct` | 0.10 | rounded up, at least one name |
| `id_keep_pct` | 0.5 | rounded up, at least one name |
| `absolute_momentum` | false | also require `r > 0` |
| `rebalance_months` | `2,5,8,11` | or `1,4,7,10`, `3,6,9,12`, monthly |
| `universe` | `""` | the lake's tickers |

Metadata: `alpha_family = "trend"`, `label_horizon_bars = 63`,
`required_history_bars = 253`. Selected names get `estimate_return = 1.0`.

## `stocks_on_the_move` (Clenow, *Stocks on the Move*)

`strategies/examples/stocks_on_the_move.py`, features in `features/trend.py`,
sizing in `portfolio/atr_parity.py`.

1. Score: annualised slope of an OLS fit of `ln(close)` over 90 sessions,
   `exp(b)^250 - 1`, times the fit's R².
2. Rank the universe and keep the top 20%. Then drop names that close below
   their 100-day average, moved 15% or more in one day within the last 90
   sessions, or score 0 or less. Dropped names keep their rank slot.
3. Index filter: while `index_ticker` closes below its 200-day average,
   make no new buys (held names are still managed). Missing index history
   also blocks new buys; set `index_ticker` to `""` to disable the filter.
4. Size with `atr_parity`: `shares = equity * risk_factor / ATR20`.
5. Trade once a week on Wednesday (Thursday if Wednesday is a holiday):
   sell names that left the selection, buy new ones best first while cash
   lasts, and resize held names every other week.

| param | default | notes |
|---|---|---|
| `lookback` | 90 | regression and gap window |
| `ma_filter` | 100 | stock trend filter |
| `gap_limit` | 0.15 | largest allowed daily move |
| `top_fraction` | 0.2 | share of the ranked universe that may be held |
| `annualization` | 250 | sessions per year for the slope |
| `atr_period` | 20 | ATR (simple mean of true range) for sizing |
| `risk_factor` | 0.001 | 10 bps of equity per ATR move |
| `index_ticker` | `SPY.US` | `""` disables the index filter |
| `index_ma` | 200 | index trend filter |
| `trade_weekday` | 2 | 0 = Monday |
| `resize_every_weeks` | 2 | Monday-anchored weeks |
| `resize_tolerance` | 0.10 | relative drift before a resize |
| `universe` | `""` | the lake's tickers, minus `index_ticker` |

Metadata: `alpha_family = "trend"`, `label_horizon_bars = 5`,
`required_history_bars = 200` (the index average).

`decide` needs that day's ATRs and index state, which `estimate_return`
computes on the same instance (backtests and lab runs). A freshly loaded
instance that never evaluated the day makes no new buys and never resizes,
but still sells names that fell out of the picks. Production sizing needs
the construction pipeline to hand ATRs to the constructor.

## `atr_parity` constructor

`get_constructor("atr_parity", risk_factor=0.001, resize_every_weeks=2,
resize_tolerance=0.10)`. Target weight `risk_factor * price / ATR`. ATRs come
from `AtrConstructionInput.atrs`; a plain `ConstructionInput` falls back to
`price * sigma_annual / sqrt(252)`. Held names with a positive signal are
funded first at their current weight (reset to parity on resize weeks when
more than the tolerance off); new names follow in score order while the
book has room. A name too big for the room left is skipped and listed in
`meta["unfunded"]`. Held names without a signal get no target and are sold.

## Deviations from the books

- **Quantitative Momentum.** The book's universe screen (liquidity and size
  filters over US stocks) is left to whoever sets `universe`. The optional
  Barroso & Santa-Clara volatility scaling from the backlog is not built in;
  use a volatility-targeting constructor or wrapper. Rebalancing on the last
  session of the month is our reading of "before quarter-end".
  `required_history_bars` is 253 (the 252-bar window plus its first close),
  not the backlog's 273.
- **Stocks on the Move.** The index filter is built in (the backlog prefers
  composing a regime wrapper; that still works with `index_ticker = ""`).
  Clenow resizes "every second Wednesday"; we use Monday-anchored week
  parity. He buys down the list until cash runs out; we skip a name too big
  for the remaining room and keep going. The ATR is a simple 20-day mean of
  true range on adjusted bars.
- Neither strategy models the book's index membership history. Use a
  point-in-time `universe` to avoid survivorship bias in the ticker list.
