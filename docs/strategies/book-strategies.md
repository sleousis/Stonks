# Book strategies

Two families from the book backlog: three cross-sectional equity strategies
(this first part) and cross-asset trend following
([below](#cross-asset-trend-following-bl-40)). The full strategy list is in
[README.md](README.md).

## Cross-sectional equity: Quantitative Momentum, Stocks on the Move and Quantitative Value

Three cross-sectional equity strategies from the book backlog
(`docs/research/book-lessons.md`, BL-38, BL-39 and BL-41). All rank a whole
universe, are long-only, and leave sizing to a `PortfolioConstructor`; the
two momentum strategies read split- and dividend-adjusted daily bars, and
`quant_value` reads point-in-time financial statements.

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

## `quant_value` (Gray & Carlisle, *Quantitative Value*)

`strategies/examples/quant_value.py`, formulas in `features/fundamentals.py`
(each function documents its formula and source). Unlike the two momentum
strategies it reads the three statement tables, point in time: a row is
visible from its `filing_date` (or `period_end + missing_filing_lag_days`
when the vendor gave none; mid-session only from the next day), through the
same readers as `quality_value`.

Default mode, `mode = "quant_value"`:

1. Universe minus `excluded_sectors` (`Financial Services`, `Financials`,
   `Utilities`, `Real Estate`; matched against `sector` and `gic_sector`,
   case-insensitive; unknown sector stays).
2. Eligible: `EBIT/TEV > 0`, a financial-strength score, and at least
   `min_years` consecutive annual reports whose latest period ended no more
   than `max_statement_age_days` ago.
3. Forensic screens, each over the eligible names that have the metric;
   drop the worst `floor(forensic_drop_pct * k)`:
   - STA `= (dCA - dCash - (dCL - dSTD - dTP) - Dep) / TA` (Sloan);
   - SNOA `= (OA - OL) / TA`, `OA = TA - cash & STI`,
     `OL = TA - STD - LTD - NCI - preferred - common equity`;
   - PMAN `= Phi(M)`, Beneish `M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI
     + 0.892 SGI + 0.115 DEPI - 0.172 SGAI + 4.679 TATA - 0.327 LVGI`;
   - distress: drop Altman `Z = 1.2 WC/TA + 1.4 RE/TA + 3.3 EBIT/TA
     + 0.6 MVE/TL + 1.0 Sales/TA` below `altman_z_min`.
4. Value: keep the cheapest `value_pct` (rounded up) by
   `EBIT / TEV`, `TEV = market cap + debt + preferred + NCI - cash & STI`.
   EBIT is trailing twelve months (or the latest annual report); balance
   items are the latest visible report; market cap is the last close times
   the latest share count.
5. Quality `= 0.5 pct(FP) + 0.5 pct(FS)` within that slice; hold the top `n`
   equally weighted (`equal_weight_top_n`).
   - Franchise power: percentile of the mean of `pct(geo-mean ROA)`,
     `pct(geo-mean ROC)`, `pct(sum FCF / TA)` and
     `max(pct(margin growth), pct(margin stability))` over the years
     available (up to `max_years`).
   - Financial strength (10 points): ROA > 0, FCF/TA > 0, FCF/TA > ROA,
     leverage fell, current ratio rose, share count fell, and ROA, FCF/TA,
     gross margin and asset turnover each rose.

`mode = "piotroski"`: the top `bm_pct` by book-to-market (positive book
only), holding F-score `>= f_min` (nine signals: ROA > 0, CFO > 0, ROA up,
CFO > NI, leverage down, current ratio up, no share issuance, gross margin
up, turnover up). `mode = "magic_formula"`: the `n` lowest rank sums of
EBIT/TEV and ROC `= EBIT / (net PP&E + CA - CL)`.

Every mode trades on the last session of `rebalance_months` (annual; a pair
such as `6,12` is semi-annual) and holds in between.

| param | default | notes |
|---|---|---|
| `mode` | `quant_value` | or `piotroski`, `magic_formula` |
| `n` | 30 | most names held |
| `value_pct` | 0.10 | cheapest slice, rounded up |
| `forensic_drop_pct` | 0.05 | per screen, rounded down |
| `altman_z_min` | 1.81 | distress cut-off |
| `min_years` / `max_years` | 3 / 8 | annual reports for franchise power |
| `bm_pct` / `f_min` | 0.2 / 7 | piotroski mode |
| `rebalance_months` | `6` | `1`..`12`, or `1,7`..`6,12` |
| `excluded_sectors` | see above | `""` disables |
| `universe` | `""` | the lake's equities |
| `missing_filing_lag_days` | 90 | lag for rows without a filing date |
| `max_statement_age_days` | 550 | older statements are ignored |

Metadata: `alpha_family = "value"`, `premise = "mean_reversion"`,
`label_horizon_bars = 252`, `required_history_bars = 1`. Features include
`ebit_tev`, `tev`, `roc`, `bm`, `sta`, `snoa`, `beneish_m`, `pman`,
`altman_z`, `fs`, `f_score`, the `fp_*` inputs and `years_used`.

**Data requirements.** Annual income statement and balance sheet (cash-flow
statement for CFO/FCF) with filing dates, for at least `min_years`
consecutive fiscal years, plus daily closes and a share count. The free
EODHD tier returns no fundamentals, so on it the strategy holds nothing;
real use needs paid fundamentals. A name with only two years still gets
STA, PMAN, FS and F (Piotroski mode works), but not franchise power, so it
is skipped in `quant_value` mode unless `min_years = 2`.

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
- **Quantitative Value.** The book's distress screen is Campbell, Hilscher
  and Szilagyi's probability of financial distress, which needs market
  volatility and excess returns; we use Altman's Z below 1.81 as the backlog
  specifies. STA has no taxes-payable term (the lake has no such line).
  Piotroski ratios use year-end rather than beginning-of-year assets, so two
  years of statements suffice. Net equity issuance is read from the change in
  share count, not buyback cash flows (vendors sign those differently). A
  signal whose inputs are missing earns no point; a forensic metric that
  can't be computed doesn't drop the name; a Beneish index with missing
  inputs is set to its neutral value (Beneish, Lee and Nichols 2013).
  Quality percentiles are taken within the cheap slice. The magic formula
  holds for the rebalance interval, not exactly 252 bars. Sectors come from
  the current instrument profile, not their history. The backlog's
  `store/lake.py` panel helper was not added: the per-ticker readers from
  `quality_value` are reused and memoised per filing state instead.
- None of the strategies models the book's index membership history. Use a
  point-in-time `universe` to avoid survivorship bias in the ticker list.

## Cross-asset trend following (BL-40)

Three time-series trend strategies and a trailing-stop wrapper from the
book backlog (`docs/research/book-lessons.md`, BL-40): Carver's EWMAC,
time-series momentum (Moskowitz, Ooi & Pedersen; Clenow) and all-time-high
trend following (Wilcox & Crittenden). Unlike the two above, each ticker is
judged on its own history, so they trade any mix of instruments.

### Shared mechanics

`strategies/examples/_forecast_trend.py`, features in
`features/forecast.py` and `features/trailing_stop.py`.

- **Forecasts.** Each strategy turns a ticker's adjusted daily bars into a
  Carver forecast: signed, averaging 10 in absolute value, capped at ±20.
  `forecast(ticker, as_of, lake)` returns it; `estimate_return` returns it
  when it is positive and `None` otherwise.
- **Long only.** A negative forecast is flat. The negative forecasts become
  the short legs once the book can short (roadmap Phase 16).
- **Sizing** goes through the `vol_target` constructor:
  `w = tau * IDM * F / 10 / sigma / N`. Here `sigma` is the EWMA (span 35)
  annualised volatility and `N` counts every ticker the strategy was asked
  about that has a forecast, flat ones included. The IDM comes from the
  tickers' last 250 daily returns. Gross is capped at 1.0, buys never spend
  more than cash plus sale proceeds, and a 10% no-trade buffer
  (`buffer_fraction`) stops small target changes from trading.
- **No look-ahead.** Bars are read through the `BarCache` on the adjusted
  basis, up to `as_of`'s session. Estimated forecast scalars and FDMs use
  only the ticker's history up to `as_of`. Week and month ends come from
  the next bar when there is one, and from the exchange calendar for the
  latest bar (crypto trades every day). A ticker whose last bar is more
  than 10 days old gets no forecast.
- **`decide`** gets no lake, so it sizes over the tickers asked on the last
  lake it saw, evaluated on its own day. A fresh instance that was asked
  nothing only sells held names that are not picked.
- Run them on daily bars with `rebalance_every_bars = 1`. The buffer and
  TSMOM's monthly refresh keep turnover down.

Shared params:

| param | default | notes |
|---|---|---|
| `tau` | 0.20 | annual volatility target of the book |
| `buffer_fraction` | 0.10 | no-trade band, as a fraction of each target |
| `scalar_mode` | `fixed` | `estimate`: 10 over the mean absolute raw signal in the ticker's history (at least 250 values) |
| `fdm_mode` | `fixed` | `estimate`: `1/sqrt(w'Hw)` from the rules' past forecasts (at least 250 rows; correlations floored at 0; capped at 2.5) |
| `fdm` | 1.1 | used when fixed, or when there is too little history |

`ath_trend` has one binary rule, so it takes only `tau` and
`buffer_fraction`.

### `ewmac_trend` (Carver, *Systematic Trading*)

`strategies/examples/ewmac_trend.py`.

1. For each fast span `f` in 8, 16, 32 and 64:
   `raw = (EMA_f - EMA_4f) / sigma_price`, where `sigma_price` is the
   zero-mean EWMA (span 36) std of daily price changes.
2. Scale by Carver's table `{8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87}` and cap
   each rule at ±20.
3. Forecast: `cap(FDM * mean(rule forecasts))`.

Each evaluation reads the last `16 * max(speeds)` bars (1024) and needs at
least `4 * max(speeds)` (256).

| param | default | notes |
|---|---|---|
| `speeds` | `8,16,32,64` | also `16,32,64`, `4,8,16,32`, `2,4,8,16,32,64`, `32,64`, `16` |
| `vol_span` | 36 | span of the price-change sigma |

Metadata: `alpha_family = "trend"`, `label_horizon_bars = 21`,
`required_history_bars = 256`. Equity, crypto and commodity.

### `tsmom` (time-series momentum)

`strategies/examples/tsmom.py`.

1. For each lookback `L` in 125 and 250: `r = ln(C_t / C_{t-L})`.
2. `mode = "sign"`: the rule forecast is `10 * sign(r)`.
   `mode = "scaled"`: `r / (sigma * sqrt(L))` times `10 / sqrt(2/pi)`
   (about 12.5, which averages 10 on a random walk), capped at ±20.
3. Forecast: `cap(FDM * mean)`. With two sign rules it is 11, 0 or -11.
4. `rebalance = "monthly"` holds the forecast from the latest month-end
   session (about every 21 bars); `"daily"` refreshes it every bar.

| param | default | notes |
|---|---|---|
| `lookbacks` | `125,250` | also `63,125,250`, `21,63,125,250`, `250`, `125` |
| `mode` | `sign` | or `scaled` |
| `vol_span` | 36 | span of the return sigma (`scaled`) |
| `rebalance` | `monthly` | or `daily` |

Metadata: `alpha_family = "trend"`, `label_horizon_bars = 21`,
`required_history_bars = 251`. Equity, crypto and commodity.

### `ath_trend` (Wilcox & Crittenden)

`strategies/examples/ath_trend.py`.

1. Entry: when flat, buy when the close of a week's last session is at or
   above every earlier close in the ticker's lake history, once there are
   at least `min_history_bars` bars.
2. Exit: sell when the close falls below
   `HWM_since_entry - k_atr * ATR(atr_bars)` (Wilder ATR). The stop never
   moves down and is checked every day. The next entry needs a new weekly
   all-time-high close.
3. Held names carry a forecast of 10. The position is replayed from the
   bars on every call; `trade_log` lists the trades.

| param | default | notes |
|---|---|---|
| `atr_bars` | 50 | 40–60 |
| `k_atr` | 10.0 | 3–12 |
| `min_history_bars` | 252 | history needed before a high counts |

Metadata: `alpha_family = "trend"`, `label_horizon_bars = 63`,
`required_history_bars = 252`. Equity and crypto.

### `TrailingStopWrapper`

`strategies/trailing_stop.py`. It follows the `FeatureRegimeFilter` and
`LastTradeFilter` pattern: params carry `inner_class_path` and
`inner_params`, and the inner's fitted state is saved under `inner/`.

- It replays the inner's long signal (`estimate_return` is not `None`) over
  the last `replay_bars` (500) bars. A trade opens when the inner turns in.
  It closes when the inner turns out, or when the close falls below
  `HWM_since_entry - k_atr * ATR(atr_period)` (3 x ATR20, never lowered).
- After a stop-out the ticker sits out `cooldown_bars` (5) bars, then
  re-enters if the inner is still in.
- While stopped out, `estimate_return` is `None`, and `decide` sells the
  position and drops the inner's orders on that ticker.
- It is stateless: the high-water mark is rebuilt from the bars on every
  call. Inner signals are memoised per lake and bar.

### Deviations from the books

- **Long only everywhere.** EWMAC and TSMOM are long/short systems in the
  books. Here short forecasts are flat until Phase 16.
- **Forecast scalars.** Carver's fixed table is the default. He fitted it
  to real, trending prices; on a driftless random walk it gives a mean
  |forecast| of about 8.2, not 10. The backlog's pooled re-estimate in the
  lab (across instruments) is not built. `scalar_mode = "estimate"` estimates per ticker instead.
- **FDM.** Fixed at 1.1 by default, Carver's value for four EWMAC speeds
  whose neighbours correlate near 0.9. The estimate mode uses the ticker's
  own rule history, not a pooled one.
- **Instrument weights.** Equal over every instrument with a forecast, as
  in Carver's static portfolio, so the book's risk grows with the number
  of longs.
- **TSMOM.** It uses log returns. The backlog's `rebalance_every_bars = 21`
  becomes a calendar month-end refresh, so it doesn't depend on the
  engine's cadence.
- **All-time high.** "All-time" means all history in the lake, so a short
  lake history misses older highs; `min_history_bars` guards fresh
  listings. Weekly closes are read from daily bars. The book's liquidity
  and price screens are left to the universe.
- **TrailingStopWrapper.** A trade already open on the replay window's
  first bar is treated as entered there, so its high-water mark (and its
  stop) can be lower than a full replay's. It is not in the lab catalog
  (`_WRAPPERS` in `lab/catalog.py` does not list it), so `stonks lab run`
  cannot name it yet; wrap a strategy with it in code.
- **File names.** The backlog's `time_series_momentum.py` and
  `features/trend_following.py` became `tsmom.py`, `features/forecast.py`
  and `features/trailing_stop.py`.

## `RegimeFilter` (BL-42)

`strategies/regime.py`, with the conditions in `features/regime_conditions.py`.
Sources: Clenow and Faber (index below its long average), Elder (Triple
Screen), Davey (bull and bear filter), Kindleberger and Lefevre (credit and
panic signs).

It wraps any strategy like the other wrappers (`inner_class_path`,
`inner_params`). Each condition says "risk off", "risk on" or "can't tell"
on a day, using only data dated on or before that day.

| Condition | Risk off when |
|-----------|---------------|
| `macro` | A `macro_indicators` series (level or change) crosses a threshold, counted from its publication date. |
| `price_trend` | A benchmark closes below its `sma`-bar average. An optional hysteresis band (up to 3%) stops flip-flopping. |
| `realized_vol` | A benchmark's realised volatility is above its own `pct` quantile over `lookback_years`. |
| `yield_curve` | Long minus short yield (`bond_yield_history`) is below `threshold`, so an inverted curve by default. |
| `higher_timeframe` | A benchmark's close is below the EMA of its weekly closes. The weeks are built from daily bars, so the current week is partial. |

Risk off when at least `k` of the n conditions trigger. Then `mode` decides:

- `block_new_buys` (default): buys are dropped, sells pass, holdings stay.
- `exit_all`: every long is sold.
- `scale`: buys shrink to the share of conditions that did not trigger.

Example params:

```json
{
  "inner_class_path": "stonks.strategies.examples.quant_momentum:QuantMomentum",
  "conditions": [
    {"kind": "price_trend", "ticker": "SPY.US"},
    {"kind": "realized_vol", "ticker": "SPY.US"},
    {"kind": "yield_curve", "long_ticker": "US10Y.GBOND", "short_ticker": "US3M.GBOND"}
  ],
  "k": 2
}
```

Tunable knobs: `k`, `trend_sma`, `trend_hysteresis`, `vol_window`,
`vol_pct` and `htf_ema`. Each fills that field of every condition whose
spec leaves it out. A new condition is one new module named
`features/regime_<name>.py` with a `RegimeCondition` subclass. No list is
edited.

Deviations from the backlog: `yield_curve` reads `bond_yield_history`, not
`macro_indicators`. A condition that can't be judged counts as not
triggered unless `when_unknown` is `trigger`.

## Legacy defaults (BL-43)

- **`momentum`.** Lookback 126 bars that end 21 bars back (`skip_days`),
  not 20 bars. Chan finds momentum at 3 to 12 months, and the last month
  tends to reverse (Gray and Vogel skip it). A param set that sets
  `lookback_days` but no `skip_days` (every one saved before) keeps
  `skip_days = 0`. The `lookback_days` bounds stay 5 to 252 so old sets
  load.
- **Breakouts.** `donchian_breakout` and `trendline_breakout` default to
  commodity, crypto and bond, with `BTC-USD.CC` as the ticker. Grimes finds
  breakouts on single stocks no better than random. Add
  `"asset_classes": ["equity"]` to run them on a stock. A saved equity
  breakout still loads but needs that opt-in to keep trading.
- **Rule DSL.** `risk.trailing_stop_vol_multiple` (0.25 to 3, Carver uses
  0.5) and `risk.trailing_stop_atr_multiple` (1 to 6) put a trailing stop
  below the best close since entry. The stop never moves down.
  `trailing_stop_period` (20) sets the window. New indicators:
  `efficiency_ratio` (Kaufman ER, default 10) and `kama` (10, 2, 30).
- **Metadata.** Every catalogued strategy now states a hypothesis, and every
  trading example an alpha family and a label horizon.
