# neurotrader888 ports: regime filters and ML strategies

Ports of ideas from the public, MIT-licensed GitHub account
[neurotrader888](https://github.com/neurotrader888). Every module is our
own implementation (no code copied); each module docstring credits its
source repo and lists every deviation. The broker is long-only, so every
short leg of the originals is dropped. All strategies declare
`applicable_asset_classes = ("crypto", "equity")` (wrappers mirror their
inner strategy).

## Complexity features (`stonks.features.complexity`)

Source: `PermutationEntropy`, `TimeSeriesReversibility`,
`TradeDependenceRunsTest`.

| Function | What it measures |
| --- | --- |
| `rolling_permutation_entropy(arr, window, d)` | Normalized permutation entropy (wraps `library.permutation_entropy`; `window` rounds to a multiple of `d!`). |
| `rolling_ptsr(arr, window, d, missing="carry")` | Zanin permutation time-series reversibility: KL divergence between the ordinal-pattern distributions of the window and of its reversal. `carry` (the original) repeats the previous value when a pattern is missing; `laplace` uses add-one smoothing. |
| `relative_async_index(arr)` / `rolling_rai(arr, window, smooth_com)` | Relative asynchronous index over horizontal-visibility-graph out-degrees of the window forward vs reversed: `-ln(min(AI_fr, AI_rf) / max(AI_fr, AI_rf))`. Optional `ewm(com=7)` smoothing. |
| `rolling_runs_z(close, lookback)` | Wald-Wolfowitz runs z of `sign(close.diff())` over the last `lookback` moves. |

Every rolling value at index `i` reads only `arr[: i + 1]`.

## ML seams (`stonks.features.ml`)

scikit-learn sits behind two small ABCs so its types never leak:

- `Classifier` (`fit`, `predict_proba -> P(y=1)`, `save`, `load`), with
  `ForestClassifier` (random forest, fixed seed).
- `Clusterer` (`fit -> Clustering(centers, labels, k, score)`), with
  `SilhouetteKMeans` (k-means for each `k` in a range, best silhouette,
  fixed seed).

`ForestClassifier.save` uses joblib (pickle). `load` reads only a fixed
file name inside the given directory, refuses symlinks and escaping names,
and checks a SHA-256 digest recorded in `model.json`. That catches corrupt
or swapped files, not an attacker who can write the whole directory, so
only load artifact directories this system wrote.

## FeatureRegimeFilter (`stonks.strategies.feature_regime`)

Wraps any strategy (`inner_class_path` + `inner_params`, like
`MacroRegimeFilter`; the inner fitted state is saved under `inner/`). For
each ticker it computes `feature` (`perm_entropy_close`,
`perm_entropy_volume`, `ptsr`, `rai`, `runs_z`) over the trailing `window`
bars, `<= as_of`. When the feature is above (`direction="risk_off_above"`)
or below (`"risk_off_below"`) `threshold`, that ticker is risk off:
`estimate_return` is `None` and `decide` sells the position and drops any
inner order on it. A feature that can't be computed counts as risk on.

Params: `feature`, `window` (tunable, 12-500), `threshold` (tunable),
`direction`, `d` (3-5, not tunable), `rai_smooth_com` (default 7, 0 turns
it off), `interval`.

Bounded look-backs: a PTSR carry goes back at most `window` windows, and
smoothed RAI applies its ewm over the last `5 * (com + 1)` values, so a
value never depends on how much history the lake holds.

## LastTradeFilter (`stonks.strategies.last_trade_filter`)

Source: `last_trade_adj_signal` in `TradeDependenceRunsTest`. Wraps any
strategy. It replays a shadow trade log of the inner strategy's own signal
(in when `estimate_return` is not `None`) over the last `replay_bars` bars.
Entries and exits are priced at signal-bar closes. A new entry is admitted
only if the previous completed round-trip was a `require_last` (`loser` by
default, or `winner`). The decision is fixed at entry and holds for the
whole trade. Exits always pass through. Long-only: a long is judged on the
previous long (the original judged it on the previous short of a
stop-and-reverse system). Inner signals are memoized per lake and bar, and
the memo is cleared on `fit`.

## PIPMinerStrategy (`stonks.strategies.examples.pip_miner`)

Source: `TechnicalAnalysisAutomation` (`pip_pattern_miner.py`,
`perceptually_important.py`, `wf_pip_miner.py`).

`fit` works on the train window only:

1. Take the z-scored `n_pips` vertical-distance PIPs of every
   `lookback`-bar window of log closes, keeping unique patterns only.
2. Cluster them with `SilhouetteKMeans` (`k_min`..`k_max`, default 5..40).
3. Score each cluster by Martin ratio (the returns of holding `hold` bars
   after each member, divided by the Ulcer index). A pattern is used only
   when its hold ends inside the train window.
4. The best cluster becomes the long cluster, provided it is profitable.

`estimate_return` returns that cluster's mean `hold`-bar return while any
of the last `hold` windows is nearest to its centroid. The fitted state
(centroids, chosen cluster, martins) is saved as JSON, never pickle.

Params: `n_pips` (3-8), `lookback` (12-96), `hold` (1-24), plus `k_min`,
`k_max`, `seed`, `interval`, `ticker`, `allocation` (not tunable).

`find_pips` is private to this module for now. It can be swapped for the
shared `features/extremes.py` implementation once that lands.

## TrendlineMetaLabelStrategy (`stonks.strategies.examples.trendline_meta_label`)

Source: `TrendlineBreakoutMetaLabel`.

- **Base entry:** the log close crosses above the resistance line fitted
  on the previous `lookback` bars.
- **Base exit:** TP of `tp_mult * ATR`, SL of `sl_mult * ATR` (ATR over
  `atr_lookback` bars of log prices), or after `hold_period` bars.
- **Fit:** builds a trade dataset from the complete base trades in the
  train window (a trade whose exit falls after `train_end` is dropped).
  Features: resistance slope / ATR, mean and max line-minus-price / ATR,
  volume / rolling median volume, and ADX(`lookback`) (implemented here).
  The label is trade return > 0. It then trains a `ForestClassifier`
  (300 trees, depth 3, fixed seed).
- **Run time:** replays the base signal over the last `3 * hold_period`
  bars. If a base trade is open and `P(win) > prob_thresh`, it returns
  `p * tp - (1 - p) * sl` in ATR units (floored slightly above 0).
  Otherwise, or while unfitted, it returns `None`.
- **Consistency:** indicators for a bar come from a fixed tail of
  `3 * max(atr_lookback, lookback)` bars ending at that bar, in both fit
  and inference, so training and live features are identical.
- **Persistence:** the model is saved with joblib under `model/` (with the
  digest check), plus `fitted_state.json`.

Params: `lookback` (24-300), `hold_period` (4-48), `tp_mult` / `sl_mult`
(1-6), `atr_lookback` (50-400), `prob_thresh` (0.5-0.8), plus
`n_estimators`, `max_depth`, `seed`, `interval`, `ticker`, `allocation`
(not tunable).
