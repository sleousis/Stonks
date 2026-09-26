# neurotrader888 indicator strategies

Six single-ticker strategies based on the public research repositories of
[neurotrader888](https://github.com/neurotrader888). Each one is Stonks' own
implementation of the published algorithm. No code was copied. The originals
use hourly BTC/ETH bars, so every bar-count parameter is in bars of the
configured `interval` (default `1h`).

What they all share (`strategies/examples/_nt888_base.py`):

- Parameters `ticker`, `interval` (default `1h`) and `allocation` (the
  fraction of cash put into a fresh long). None of the three is tunable.
- `applicable_asset_classes = ("crypto", "equity")`.
- **Long or flat only.** The broker cannot short, so wherever an original
  goes short, the port goes flat.
- `estimate_return` gives a positive magnitude proxy while the signal is
  long and `None` otherwise. It is not a forecast return. It exists so the
  Ranker can order picks, and it is floored at `1e-6` so a long signal
  always clears the backtester's default `threshold=0`. `decide` buys when
  the strategy is picked and flat, and sells the whole position when it is
  not picked.
- Look-ahead safety: every value is computed from bars with
  `timestamp <= as_of`, read through `BarCache`. Stateful rules are replayed
  over a trailing window of bars on every call, so a result depends only on
  `as_of` and never on how often or in what order the strategy was called.
  Tests check that adding future bars never changes a past answer.
- Warm-up: the result is `None` (and features are `{}`) until there is
  enough history. Zero ATRs or medians become NaN, never `inf`, and NaN never
  triggers an entry or an exit.

The helpers use numpy and pandas only: `features/visibility_graph.py`,
`features/market_profile.py` (weighted KDE, peak prominence) and
`features/vsa.py` (OLS, rolling deviation).

## VolatilityHawkesStrategy (`volatility_hawkes`)

- **Source:** neurotrader888/VolatilityHawkes, `hawkes.py`. MIT License.
- **Indicator:** `norm_range = (log H - log L) / ATR_rma(log prices, norm_lookback)`,
  `v = hawkes_process(norm_range, kappa)`, and `q05` / `q95` are the rolling
  `quantile_lookback` quantiles of `v`.
- **Rule:** when `v < q05`, remember the bar as `last_below` and go flat.
  When `v` crosses above `q95`, go long if `close > close[last_below]`
  (the original goes short otherwise; here that is flat). Hold until the
  next dip below `q05`.
- **Params:** `kappa` 0.01–0.5 (default 0.1), `quantile_lookback` 24–336
  (168), `norm_lookback` 50–500 (336, not tunable).
- **estimate_return:** `close / close[last_below] - 1`.
- **Deviations:**
  - Short becomes flat.
  - The state is replayed over the trailing
    `2*norm_lookback + 4*quantile_lookback` bars, so the ATR and Hawkes
    warm-up starts at the beginning of that window.
  - A calm bar at index 0 counts (`>= 0` instead of `> 0`).

## VisibilityGraphPathStrategy (`visibility_graph_path`)

- **Source:** neurotrader888/TimeSeriesVisibilityGraphs,
  `network_indicators.py`. MIT License.
- **Indicator:** build natural visibility graphs of the last `lookback`
  closes and of their negation, then take the average shortest path length
  of each (`path_pos`, `path_neg`).
- **Rule:** long when `path_pos > path_neg`, flat otherwise (the original's
  short becomes flat).
- **Params:** `lookback` 6–48 (default 12).
- **estimate_return:** `path_pos - path_neg`.
- **Deviations:**
  - No `ts2vg` or `networkx`. Edges come from an O(n²) slope sweep and path
    lengths from BFS, both our own.
  - Time is the bar index.

## VSAStrategy (`vsa`)

- **Source:** neurotrader888/VSAIndicator, `vsa.py`. MIT License.
- **Indicator:** `norm_range = (H - L) / ATR_rma(n)` and
  `norm_volume = V / rolling_median(V, n)`. OLS of `norm_range` on
  `norm_volume` over the previous `n` bars. `dev` is the current bar's actual
  normalised range minus the fitted one, or 0 if `slope <= 0` or `r < 0.2`.
- **Rule (a Stonks addition; the original has no trading rule):**
  - `mode="absorption"` (the default): `dev < -threshold` (a small range on
    heavy volume) triggers a long entry.
  - `mode="expansion"`: `dev > threshold` triggers instead.
  - The position is held for `hold_bars` bars from the latest trigger. A new
    trigger restarts the count.
- **Params:** `norm_lookback` 48–504 (default 168), `threshold` 0.5–2.0
  (1.0), `hold_bars` 1–48 (24), `mode` (not tunable).
- **estimate_return:** `|dev|` of the triggering bar.
- **Deviations:**
  - **The fit for bar i uses bars [i-n, i-1] and excludes bar i itself.**
    The original includes the bar in its own fit, which pulls the line
    towards it and shrinks every deviation.
  - The rolling regression uses vectorised rolling sums.
  - A window where volume doesn't vary gives 0 instead of NaN.
  - Recomputed over the trailing `2n + hold_bars` bars.

## MarketProfileSRStrategy (`market_profile_sr`)

- **Source:** neurotrader888/TechnicalAnalysisAutomation,
  `mp_support_resist.py`. MIT License.
- **Indicator:** a weighted Gaussian KDE of the last `lookback` log closes.
  Weights rise linearly from `first_w` to 1. The KDE is evaluated on a
  200-point grid, and its bandwidth factor is `log ATR(lookback) * atr_mult`
  (scipy semantics: kernel sigma = factor × weighted std of the sample).
  Levels are the density peaks with prominence `>= prom_thresh * max density`.
- **Rule:** long when the close crosses up through any level, flat when it
  crosses down through one (the original goes short), otherwise hold. When
  one bar crosses several levels, the last level in ascending order decides.
- **Params:** `lookback` 100–500 (default 365), `first_w` 0.01–1 (0.01),
  `atr_mult` 1–5 (3), `prom_thresh` 0.1–0.5 (0.25).
- **estimate_return:** `close / level_crossed - 1`.
- **Deviations:**
  - No scipy. The KDE (including scipy's effective-sample-size variance),
    `find_peaks` and the prominence calculation are reimplemented.
  - The grid has exactly 200 points.
  - The ATR at bar i is computed over the `2*lookback` bars ending at i, not
    the full history, so each bar's levels depend on a fixed window and are
    safe to cache.
  - The state is replayed over the trailing `lookback` bars.
  - Levels are cached per (lake, ticker, interval, bar timestamp) on the
    instance, so a backtest runs one KDE per new bar.
  - Degenerate windows give no levels.

## IntramarketDifferenceStrategy (`intramarket_difference`)

- **Source idea:** neurotrader888/IntramarketDifference. **That repository
  has no license**, so none of its code was read or used. This is a
  clean-room implementation written from the algorithm description only.
- **Indicator:** `cmma = (close - SMA(close, lookback)) / (ATR_rma(atr_lookback) * sqrt(lookback))`
  and `diff = cmma(ticker) - cmma(reference_ticker)`. Both are computed on
  the timestamps the two markets share (inner join before any indicator is
  computed).
- **Rule:** go long when `diff > threshold` and exit when `diff <= 0`. The
  short side is flat.
- **Params:** `lookback` 6–168 (default 24), `threshold` 0.05–1.0 (0.25),
  `atr_lookback` 24–336 (168), `reference_ticker` (default `BTC-USD.CC`, not
  tunable). The default `ticker` is `ETH-USD.CC`.
- **estimate_return:** `diff`, which is always > 0 while long.
- **Choices:**
  - The state is replayed over the last `4*atr_lookback` traded bars.
  - `None` when the reference has no bars, or no bar at the traded ticker's
    latest timestamp (a stale reference).
  - The reference ticker needs bars in the lake but does not need to be in
    the traded universe.

## MACrossoverStrategy (`ma_crossover`)

- **Source:** neurotrader888/mcpt, `moving_average.py`. MIT License.
- **Rule:** long while `SMA(close, fast) > SMA(close, slow)`, flat otherwise.
- **Params:** `fast` 2–50 (default 10), `slow` 10–200 (30).
  `fast >= slow` raises `ValueError`, and the tuners record that as a failed
  trial.
- **estimate_return:** `fast_ma / slow_ma - 1`.
- **Deviations:** the `fast < slow` check. The original hard-codes 10/30.
