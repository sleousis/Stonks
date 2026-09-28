"""PIPMinerStrategy — mine perceptually-important-point (PIP) patterns and
trade the historically best cluster.

``fit(dataset)`` on the train window only:

1. Take log closes. For every ``lookback``-bar window, find ``n_pips``
   perceptually important points (vertical-distance PIPs) and z-score their
   prices. Consecutive windows whose interior PIPs sit on the same bars are
   the same pattern; only the first is kept.
2. Cluster the unique patterns with k-means, choosing ``k`` in
   ``[k_min, k_max]`` by silhouette (behind the
   :class:`~stonks.features.ml.Clusterer` seam, fixed seed).
3. Score every cluster by its Martin ratio (sum of the next-bar log
   returns while holding ``hold`` bars after each member / Ulcer index of
   that equity curve). Patterns are only taken where the whole hold ends
   inside the train window, so no return after ``train_end`` is ever read.
4. The best-scoring cluster is the long cluster (the worst would be the
   short cluster; the broker is long-only so it is ignored). If even the
   best cluster loses money, or its members' mean ``hold``-bar return is
   not positive, the strategy never goes long.

``estimate_return``: when any of the last ``hold`` windows (ending at
``as_of`` or up to ``hold - 1`` bars earlier) is nearest to the long
cluster's centroid, return that cluster's mean ``hold``-bar log return;
otherwise ``None`` (which makes ``decide`` exit).

Persistence: centroids and the chosen cluster are saved as JSON
(``fitted_state.json``), never pickle.

Source: ``pip_pattern_miner.py`` / ``perceptually_important.py`` /
``wf_pip_miner.py`` in neurotrader888's MIT licensed
``TechnicalAnalysisAutomation`` repo. Own implementation, no code copied.
Deviations:

- scikit-learn k-means (k-means++, ``n_init`` restarts, fixed seed) and
  silhouette instead of ``pyclustering``; silhouettes are scored on at most
  3000 sampled patterns.
- Long-only: the short cluster is not traded.
- The signal at ``as_of`` is derived statelessly from the last ``hold``
  windows instead of a running hold counter (same result bar by bar).
- The returned estimate is the long cluster's mean ``hold``-bar return (the
  original emitted a +1 / -1 signal).
- Flat windows (zero PIP price spread) can't be z-scored and are skipped.
- ``find_pips`` here is private to this port; ``features/extremes.py`` (in
  progress elsewhere) may replace it.
- The original's Monte Carlo permutation test lives in the lab's survival
  suite, not in the strategy.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.ml import SilhouetteKMeans
from stonks.strategies._common import LakeBarCaches
from stonks.strategies._wrapping import INTERVALS
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._nt888_common import long_only_decide, train_bars

_STATE_FILE = "fitted_state.json"
_MEMO_MAX = 50_000


def find_pips(data: np.ndarray, n_pips: int) -> tuple[list[int], list[float]]:
    """Indices and values of ``n_pips`` perceptually important points of
    ``data`` (vertical distance): start with both endpoints, then repeatedly
    add the point farthest (vertically) from the line joining its adjacent
    PIPs. Ties go to the earliest point."""
    data = np.asarray(data, dtype=float)
    if n_pips < 2 or len(data) < n_pips:
        raise ValueError(f"need 2 <= n_pips <= len(data), got {n_pips} for {len(data)}")
    xs = [0, len(data) - 1]
    for _ in range(n_pips - 2):
        best_d, best_i, insert_at = -1.0, -1, -1
        for seg in range(len(xs) - 1):
            left, right = xs[seg], xs[seg + 1]
            if right - left < 2:
                continue
            idx = np.arange(left + 1, right)
            slope = (data[right] - data[left]) / (right - left)
            dist = np.abs(data[left] + slope * (idx - left) - data[idx])
            j = int(np.argmax(dist))
            if dist[j] > best_d:
                best_d, best_i, insert_at = float(dist[j]), int(idx[j]), seg + 1
        if best_i < 0:
            break
        xs.insert(insert_at, best_i)
    return xs, [float(data[i]) for i in xs]


def _zscore(values: Sequence[float]) -> np.ndarray | None:
    arr = np.asarray(values, dtype=float)
    std = arr.std()
    if not std > 0:
        return None
    return (arr - arr.mean()) / std


def _martin(rets: np.ndarray) -> float:
    """Martin ratio of a per-bar log-return stream (sign kept: a losing
    stream scores negative)."""
    total = float(np.sum(rets))
    sign = -1.0 if total < 0 else 1.0
    equity = np.exp(np.cumsum(sign * rets))
    drawdown = equity / np.maximum.accumulate(equity) - 1.0
    ulcer = float(np.sqrt(np.mean(drawdown**2)))
    return sign * abs(total) / max(ulcer, 1e-12)


class PIPMinerStrategy(BaseStrategy):
    id = "pip_miner"
    title = "Price pattern miner"
    summary = (
        "Trades price shapes that past data says are followed by better returns. Purely "
        "statistical."
    )
    hypothesis = (
        "Some price shapes, found by clustering past patterns, are "
        "followed by better returns than others. The edge is purely "
        "statistical and fails when the clusters were fitted noise."
    )
    alpha_family = "data_driven"
    premise = "none"
    label_horizon_bars = 6
    required_history_bars = 24

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "label_horizon_bars": int(p["hold"]),
            "required_history_bars": int(p["lookback"]),
        }

    applicable_asset_classes = ("crypto", "equity")

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="n_pips", kind="int", default=5, bounds=(3, 8), description="PIPs per pattern."
            ),
            ParameterSpec(
                name="lookback",
                kind="int",
                default=24,
                bounds=(12, 96),
                description="Bars per pattern window.",
            ),
            ParameterSpec(
                name="hold",
                kind="int",
                default=6,
                bounds=(1, 24),
                description="Bars held after a pattern fires.",
            ),
            ParameterSpec(
                name="k_min",
                kind="int",
                default=5,
                bounds=(2, 40),
                tunable=False,
                description="Smallest cluster count tried.",
            ),
            ParameterSpec(
                name="k_max",
                kind="int",
                default=40,
                bounds=(2, 80),
                tunable=False,
                description="Largest cluster count tried.",
            ),
            ParameterSpec(
                name="seed",
                kind="int",
                default=0,
                bounds=(0, 2**31 - 1),
                tunable=False,
                description="k-means seed (fit is deterministic for a seed).",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=list(INTERVALS),
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="BTC-USD.CC",
                bounds=None,
                tunable=False,
                description="Ticker the strategy trades.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash deployed on a fresh long entry.",
            ),
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        if int(self.params["k_max"]) < int(self.params["k_min"]):
            raise ValueError("k_max must be >= k_min")
        self._centroids: np.ndarray | None = None
        self._long_cluster: int | None = None
        self._long_mean_return: float | None = None
        self._cluster_martins: list[float] = []
        self._cluster_sizes: list[int] = []
        self._bar_caches = LakeBarCaches()
        self._memo: dict[bytes, int | None] = {}

    # ---- fitted state ----------------------------------------------------------

    @property
    def is_fitted(self) -> bool:
        return self._centroids is not None

    def fitted_state(self) -> dict[str, Any]:
        if not self.is_fitted:
            return {}
        return {
            "centroids": self._centroids.tolist(),
            "long_cluster": self._long_cluster,
            "long_mean_return": self._long_mean_return,
            "cluster_martins": list(self._cluster_martins),
            "cluster_sizes": list(self._cluster_sizes),
        }

    def _set_state(self, state: Mapping[str, Any]) -> None:
        self._centroids = np.asarray(state["centroids"], dtype=float)
        lc = state["long_cluster"]
        self._long_cluster = None if lc is None else int(lc)
        lm = state["long_mean_return"]
        self._long_mean_return = None if lm is None else float(lm)
        self._cluster_martins = [float(m) for m in state["cluster_martins"]]
        self._cluster_sizes = [int(n) for n in state["cluster_sizes"]]
        self._memo = {}

    # ---- fit --------------------------------------------------------------------

    def fit(self, dataset: Any) -> None:
        interval = Interval.parse(self.params["interval"])
        bars = train_bars(dataset, self.params["ticker"], interval, caches=self._bar_caches)
        log_close = np.log(bars["close"].astype(float).to_numpy())
        n_pips = int(self.params["n_pips"])
        lookback = int(self.params["lookback"])
        hold = int(self.params["hold"])

        patterns: list[np.ndarray] = []
        ends: list[int] = []
        last_interior: list[int] | None = None
        # a pattern at bar i is used only if its hold ends inside the window
        for i in range(lookback - 1, len(log_close) - hold):
            start = i - lookback + 1
            xs, ys = find_pips(log_close[start : i + 1], n_pips)
            interior = [x + start for x in xs[1:-1]]
            if interior == last_interior:
                continue
            last_interior = interior
            z = _zscore(ys)
            if z is not None:
                patterns.append(z)
                ends.append(i)
        k_min = int(self.params["k_min"])
        if len(patterns) <= k_min:
            raise ValueError(
                f"only {len(patterns)} unique patterns in the training window; "
                f"need more than k_min={k_min}"
            )

        clustering = SilhouetteKMeans(
            k_min=k_min, k_max=int(self.params["k_max"]), seed=int(self.params["seed"])
        ).fit(np.vstack(patterns))

        next_ret = np.diff(log_close)  # next_ret[i] = log_close[i+1] - log_close[i]
        ends_arr = np.asarray(ends)
        martins: list[float] = []
        sizes: list[int] = []
        mean_returns: list[float] = []
        for c in range(clustering.k):
            members = ends_arr[clustering.labels == c]
            held = np.zeros(len(next_ret))
            for i in members:
                held[i : i + hold] = 1.0
            martins.append(_martin(held * next_ret) if len(members) else float("-inf"))
            sizes.append(len(members))
            fwd = log_close[members + hold] - log_close[members]
            mean_returns.append(float(fwd.mean()) if len(members) else 0.0)

        best = int(np.argmax(martins))
        long_cluster: int | None = best
        if not martins[best] > 0 or not mean_returns[best] > 0:
            long_cluster = None
        self._set_state(
            {
                "centroids": clustering.centers,
                "long_cluster": long_cluster,
                "long_mean_return": None if long_cluster is None else mean_returns[best],
                "cluster_martins": [m if np.isfinite(m) else -1e308 for m in martins],
                "cluster_sizes": sizes,
            }
        )

    # ---- inference ----------------------------------------------------------------

    def nearest_cluster(self, log_window: np.ndarray) -> int | None:
        """Cluster whose centroid is nearest to the window's z-scored PIPs
        (``None`` when unfitted or the window is flat)."""
        if not self.is_fitted:
            return None
        window = np.asarray(log_window, dtype=float)
        key = window.tobytes()
        if key in self._memo:
            return self._memo[key]
        _, ys = find_pips(window, int(self.params["n_pips"]))
        z = _zscore(ys)
        result = None if z is None else int(np.argmin(np.linalg.norm(self._centroids - z, axis=1)))
        if len(self._memo) > _MEMO_MAX:
            self._memo.clear()
        self._memo[key] = result
        return result

    def _active(self, ticker: str, as_of: Any, lake: Any) -> bool:
        if not self.is_fitted or self._long_cluster is None or lake is None:
            return False
        if ticker != self.params["ticker"]:
            return False
        lookback = int(self.params["lookback"])
        hold = int(self.params["hold"])
        interval = Interval.parse(self.params["interval"])
        closes = self._bar_caches.for_lake(lake).last_n_closes(
            ticker, interval, as_of, lookback + hold - 1
        )
        if len(closes) < lookback or np.any(~(closes > 0)):
            return False
        log_close = np.log(closes)
        n = len(log_close)
        for k in range(min(hold, n - lookback + 1)):
            end = n - k
            if self.nearest_cluster(log_close[end - lookback : end]) == self._long_cluster:
                return True
        return False

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        if not self.is_fitted:
            return Features(values={})
        return Features(values={"pip_signal": 1.0 if self._active(ticker, as_of, lake) else 0.0})

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if not self._active(ticker, as_of, lake):
            return None
        return self._long_mean_return

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        return long_only_decide(
            self.id,
            self.params["ticker"],
            self.params["allocation"],
            my_picks,
            portfolio,
            prices,
            as_of,
        )

    # ---- persistence --------------------------------------------------------------

    def save(self, path: Path) -> None:
        super().save(path)
        if self.is_fitted:
            (Path(path) / _STATE_FILE).write_text(
                json.dumps(self.fitted_state(), indent=2, sort_keys=True)
            )

    @classmethod
    def load(cls, path: Path) -> PIPMinerStrategy:
        instance: PIPMinerStrategy = super().load(path)  # type: ignore[assignment]
        state_file = Path(path) / _STATE_FILE
        if state_file.exists():
            instance._set_state(json.loads(state_file.read_text()))
        return instance
