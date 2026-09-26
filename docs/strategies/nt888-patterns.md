# Chart-pattern strategies ported from neurotrader888

Four single-ticker, long-only strategies and one feature module, re-implemented
from the MIT-licensed research of [neurotrader888](https://github.com/neurotrader888)
(Copyright (c) neurotrader888). The code is our own; the algorithms, and the
harmonic ratio templates, come from:

- [`TechnicalAnalysisAutomation`](https://github.com/neurotrader888/TechnicalAnalysisAutomation)
  (`rolling_window.py`, `directional_change.py`, `perceptually_important.py`,
  `head_shoulders.py`, `flags_pennants.py`, `harmonic_patterns.py`)
- [`market-structure`](https://github.com/neurotrader888/market-structure)
  (`atr_directional_change.py`, `hierarchical_extremes.py`)

## What they share

- **Where:** `src/stonks/strategies/examples/`, so the catalog finds them.
  Swing-point detectors live in `src/stonks/features/extremes.py`.
- **Asset classes:** `("crypto", "equity")`. The originals were researched on
  hourly BTC, so every length parameter is a number of **bars** at the
  configured `interval`, whatever that interval is.
- **Common params:** `ticker`, `interval` (default `1h`) and `allocation`
  (share of cash spent on an entry), none of them tunable.
- **Base class:** all four subclass `SingleTickerLongFlat` from
  `strategies/examples/_nt888_base.py`, shared with the nt888 indicator ports.
  Each one only implements `_evaluate`, which returns the state (with `signal`
  and `score`) at `as_of`.
- **Orders:** `decide` buys with `allocation` of cash when the ticker is picked
  and the strategy is flat, and sells the whole position when it is not picked.
  The Stonks broker is long-only, so every short leg of the originals is simply
  flat. `estimate_return` is `None` while flat and a positive magnitude while a
  trade is live.
- **No look-ahead:** every extreme has a `conf_index` (the first bar it can be
  known) as well as its `ext_index`. A strategy standing at bar `i` only uses
  extremes with `conf_index <= i`. Tests check that each detector run on
  `data[:n]` reports exactly the full run's extremes with `conf_index < n`.
- **Same state in backtest and production:** nothing is kept between calls.
  Each call replays a fixed number of the latest bars (`history_bars()`) from
  scratch, so a fresh production instance and a backtest that stepped through
  every bar agree for the same `as_of`. The head & shoulders and flag strategies
  also skip a pattern when the bars it depends on would slide out of that window
  before its hold ends, so a trade never disappears halfway through. The
  harmonic and market-structure strategies restart their zig-zag at the start of
  the window. Their state is still deterministic for a given `as_of`, but a
  swing point just after the window start can differ from what a run over the
  full history would find. The windows (2000 and at least 3000 bars) are long
  so that this rarely matters.

## Feature module: `features/extremes.py`

| Helper | Rule |
|---|---|
| `rw_extremes(close, order)` | Bar `k` is a top (bottom) when its close is `>=` (`<=`) every close within `order` bars on each side. Confirmed at `k + order`. |
| `directional_change(high, low, close, sigma)` / `DirectionalChange` | Zig-zag. A top is confirmed when the close falls below the running max of highs times `1 - sigma`, and a bottom when the close rises above the running min of lows times `1 + sigma`. |
| `atr_directional_change(high, low, close, atr_lookback)` / `ATRDirectionalChange` | Same zig-zag, but the threshold is the simple-mean ATR: a top is confirmed when the low falls below the pending max minus the ATR. The ATR warm-up bars are skipped. |
| `HierarchicalExtremes(levels=5)` | A level-L extreme is promoted to level L+1 when it beats the same-type extremes on both sides of it. It is confirmed on the bar its right-hand neighbour is confirmed. When two same-type extremes would end up next to each other, the most extreme opposite point between them is inserted so each level keeps alternating. `level_high/low(_price)(level, lag)` return the last confirmed ones. `hierarchical_level_prices(...)` gives them for every bar. |
| `find_pips(data, n_pips, dist_measure)` | Perceptually important points. Starts from the two endpoints and keeps inserting the point farthest from the line between its neighbours. Distance: 1 = euclidean, 2 = perpendicular, 3 = vertical. |

Deviations from the original:

- `rw_extremes` confirms from bar `2 * order` on. The original skipped that
  first possible bar.
- The ATR comes from `features.indicators.atr(method="sma")` instead of a
  hand-kept running sum. The values are the same.
- `HierarchicalExtremes` accepts base extremes from any detector, and tracks
  bar indices without timestamps.
- `find_pips` returns fewer points when no interior point is left. The
  original inserted index `-1` in that case.

## HeadShouldersStrategy (`head_shoulders`)

**Source:** `TechnicalAnalysisAutomation/head_shoulders.py`.

**Rule (inverse head & shoulders, on log closes):** take the last four
alternating rolling-window extremes that end with a top: left shoulder, left
armpit, head and right armpit. The right shoulder is the lowest close since the
right armpit. The pattern counts when:

- the head is below both shoulders;
- each shoulder is below the midpoint between the other shoulder and its armpit
  (balance);
- the two shoulder-to-head times are within 2.5x of each other (symmetry);
- somewhere within one head width before the left shoulder, price was above the
  neckline (the line through the two armpits).

Buy on the first bar that closes above the neckline projected to that bar. With
`early_find`, buy instead on the first close above the midpoint of the right
shoulder and right armpit. Exit on whichever comes first:

- a close at or above `neckline + head height` (target);
- a close at or below the right shoulder (stop);
- `round(head_width * hold_mult)` bars.

| Param | Kind | Default | Bounds |
|---|---|---|---|
| `order` | int | 6 | 2 – 48 |
| `early_find` | bool | False | — |
| `hold_mult` | float | 1.0 | 0.5 – 3.0 |

**Deviations:**

- The regular (bearish) pattern is not traded.
- The close must be strictly above the trigger level.
- No new pattern is taken while a trade is live.
- The original only used the stop and target to measure returns. Here they are
  the exit rule, and the time stop is scaled by `hold_mult`.
- The R-squared attribute is dropped.
- History is limited to `100 * order + 200` bars, with the window-stability
  check described above.

## FlagPennantStrategy (`flag_pennant`)

**Source:** `TechnicalAnalysisAutomation/flags_pennants.py`.

**Rule (bull flags and pennants, on log prices):**

- `variant="pips"`: the pole runs from the last confirmed rolling-window bottom
  to the highest close since then. The flag is everything after the pole's tip.
  It must be at least `max(5, order/2)` bars long, and no more than half the
  pole's width or half its height. Take 5 PIPs of the flag (vertical distance);
  the middle one must sit above its two neighbours. Resistance runs through
  PIPs 0 and 2, support through PIPs 1 and 3. The pattern is rejected when the
  lines cross inside the flag, or less than one flag width behind the tip (they
  diverge too sharply). Buy when the close is above resistance.
- `variant="trendline"`: when a top is confirmed, the pole runs from the last
  confirmed bottom to that top. On each later bar the flag, from the tip up to
  the previous bar, may not rise above the tip. It must be no more than half the
  pole's width or three quarters of its height. Fit support and resistance to
  the flag's highs and lows with `fit_trendlines_high_low`, and buy when the
  close is above resistance projected to the current bar.
- Either way, the position is held for `int(flag_width * hold_mult)` bars (at
  least 1).

| Param | Kind | Default | Bounds |
|---|---|---|---|
| `variant` | categorical | `pips` | `pips`, `trendline` |
| `order` | int | 12 | 3 – 48 |
| `hold_mult` | float | 1.0 | 0.5 – 3.0 |

**Deviations:**

- Bear patterns are not traded.
- The trendline variant fits highs and lows instead of closes only, and projects
  resistance to the current bar (x = flag width). The original projected to
  `flag_width + 1`, one bar too far.
- Breakouts must be strictly above resistance.
- One trade at a time.
- The hold period was a research measurement in the original; here it is the
  exit rule.
- `estimate_return` targets the classic measured move: the breakout close plus
  the pole height.
- History is limited to `60 * order + 200` bars, with the window-stability
  check.

## HarmonicXABCDStrategy (`harmonic_xabcd`)

**Source:** `TechnicalAnalysisAutomation/harmonic_patterns.py`. The ratio
templates are copied from it:

| Pattern | AB/XA | BC/AB | CD/BC | AD/XA |
|---|---|---|---|---|
| Gartley | 0.618 | 0.382–0.886 | 1.13–1.618 | 0.786 |
| Bat | 0.382–0.50 | 0.382–0.886 | 1.618–2.618 | 0.886 |
| Butterfly | 0.786 | 0.382–0.886 | 1.618–2.24 | 1.27–1.41 |
| Crab | 0.382–0.618 | 0.382–0.886 | 2.618–3.618 | 1.618 |
| Deep Crab | 0.886 | 0.382–0.886 | 2.0–3.618 | 1.618 |
| Cypher | 0.382–0.618 | 1.13–1.41 | 1.27–2.00 | 0.786 |
| Shark | — | 1.13–1.618 | 1.618–2.24 | 0.886–1.13 |

**Rule (bullish patterns only):**

- Find swing points with a percentage directional change (`sigma`) on
  highs, lows and closes.
- When the last confirmed extreme is a top, the last four extremes are X, A, B
  and C, and price is on the CD leg. On each bar whose low is the lowest since C
  was confirmed, that low is the candidate D.
- Score each template on the four ratios:
  - an exact target costs `|log(actual/target)|`;
  - a range costs 0 inside it, and twice the log distance to the nearest edge
    outside it;
  - a missing ratio costs 0.
- Buy at D's close when the best template scores within `err_thresh`. Hold until
  the next directional-change extreme is confirmed. There is one entry per CD
  leg.

| Param | Kind | Default | Bounds |
|---|---|---|---|
| `sigma` | float | 0.02 | 0.005 – 0.06 |
| `err_thresh` | float | 0.2 | 0.1 – 0.75 |

**Deviations:**

- Bear patterns are not traded.
- The replay is strictly causal: the original loop peeked at the next extreme's
  confirmation bar.
- `estimate_return` needs a size, which the original never defined. It uses a
  0.382 retracement of CD as the target.
- The directional change restarts at the start of a 2000-bar window.

## MarketStructureBreakStrategy (`market_structure_break`)

**Source:** `market-structure/hierarchical_extremes.py` (the detector). The
breakout rule on top of it is ours.

**Rule:** build hierarchical extremes from an ATR directional change with a
simple-mean ATR over `atr_lookback` bars. Go long when the close is above the
last confirmed level-`level` high. Go flat when the close is below the last
confirmed level-`level` low. Otherwise keep the current position. There is no entry until the level
has a confirmed high.

| Param | Kind | Default | Bounds |
|---|---|---|---|
| `atr_lookback` | int | 24 | 14 – 500 |
| `level` | int | 1 | 0 – 4 |

**Deviations:**

- A break below the last low goes flat, not short.
- The structure is rebuilt from the start of a `max(3000, 20 * atr_lookback)`
  bar window.
- `estimate_return` is the close's distance above the level high.
