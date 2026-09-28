"""FeatureRegimeFilter — gate any strategy on a per-ticker complexity feature.

Wraps an inner :class:`Strategy` (same pattern as
:class:`~stonks.strategies.macro_regime.MacroRegimeFilter`: params carry
``inner_class_path`` + ``inner_params``, the inner strategy's fitted state
is saved under ``inner/`` and restored on load). For every ticker it
computes one feature over that ticker's trailing ``window`` bars, using
only bars with ``timestamp <= as_of``:

- ``perm_entropy_close`` / ``perm_entropy_volume``: normalized permutation
  entropy (order ``d``) of closes / volumes;
- ``ptsr``: permutation time-series reversibility of closes;
- ``rai``: relative asynchronous index of closes (optionally
  ``ewm(com=rai_smooth_com)`` smoothed);
- ``runs_z``: Wald-Wolfowitz runs z-score of close-to-close move signs.

Risk off when the feature is above (``direction="risk_off_above"``) or
below (``"risk_off_below"``) ``threshold``. In risk off
``estimate_return`` is ``None`` for that ticker and ``decide`` sells the
whole position in it (and drops any inner order on it). A feature that
can't be computed (too little history, degenerate window) counts as risk on.

Source: neurotrader888's MIT licensed ``PermutationEntropy``,
``TimeSeriesReversibility`` and ``TradeDependenceRunsTest`` repos, which
present these as indicators; using them as a risk-off gate is this port's
own framing. Own implementation, no code copied. Deviations:

- The carried-forward PTSR value (a window with a missing ordinal pattern
  repeats the previous value) looks back at most ``window`` windows; older
  carries are dropped and the feature counts as unknown.
- Smoothed RAI applies the ewm over the last ``5 * (com + 1)`` window
  values only (:meth:`smoothing_tail`), so the value at ``as_of`` doesn't
  depend on how much history the lake holds.
- Permutation-entropy windows round to a multiple of ``d!`` (see
  :func:`stonks.features.complexity.rolling_permutation_entropy`).
"""

from __future__ import annotations

import math
import weakref
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.complexity import ptsr, relative_async_index, rolling_permutation_entropy
from stonks.features.library import runs_test_z_score
from stonks.strategies._common import LakeBarCaches, iso
from stonks.strategies._wrapping import INTERVALS, InnerStrategyWrapper, inner_param_specs

FEATURES = ["perm_entropy_close", "perm_entropy_volume", "ptsr", "rai", "runs_z"]
_MEMO_MAX = 50_000


class FeatureRegimeFilter(InnerStrategyWrapper):
    id = "feature_regime_filter"
    title = "Orderly market filter"
    summary = "Wraps another strategy and lets it trade only while prices move in an orderly way."
    hypothesis = (
        "Some strategies only work when a market is orderly, and entropy "
        "style features tell orderly from random. Gating on them cuts "
        "losing trades. Adds no alpha of its own."
    )
    id_suffix = "feature_regime"

    @classmethod
    def parameter_spec(cls):
        return [
            *inner_param_specs(),
            ParameterSpec(
                name="feature",
                kind="categorical",
                default="perm_entropy_close",
                bounds=list(FEATURES),
                tunable=False,
                description="Per-ticker feature the regime is judged on.",
            ),
            ParameterSpec(
                name="window",
                kind="int",
                default=100,
                bounds=(12, 500),
                description="Trailing bars the feature is computed over.",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=0.9,
                bounds=(-10.0, 10.0),
                description="Risk off when the feature crosses this level. Scales "
                "differ: entropy in [0, 1], PTSR/RAI >= 0, runs z roughly [-4, 4].",
            ),
            ParameterSpec(
                name="direction",
                kind="categorical",
                default="risk_off_below",
                bounds=["risk_off_above", "risk_off_below"],
                tunable=False,
                description="Risk off when the feature is above / below threshold.",
            ),
            ParameterSpec(
                name="d",
                kind="int",
                default=3,
                bounds=(3, 5),
                tunable=False,
                description="Ordinal-pattern length (entropy and PTSR).",
            ),
            ParameterSpec(
                name="rai_smooth_com",
                kind="float",
                default=7.0,
                bounds=(0.0, 50.0),
                tunable=False,
                description="ewm centre of mass smoothing RAI; 0 disables.",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=list(INTERVALS),
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()
        # per lake: (ticker, last bar timestamp) -> feature value
        self._values: weakref.WeakKeyDictionary[Any, dict] = weakref.WeakKeyDictionary()
        # pure per-window results keyed by the window's bytes
        self._memo: dict[tuple[str, bytes], float] = {}

    # ---- feature ------------------------------------------------------------

    def smoothing_tail(self) -> int:
        """Window values the smoothed RAI's ewm runs over (1 = unsmoothed)."""
        com = float(self.params["rai_smooth_com"])
        return 1 if com <= 0 else int(math.ceil(5 * (com + 1)))

    def _tail_bars(self) -> int:
        feature = self.params["feature"]
        window = int(self.params["window"])
        d = int(self.params["d"])
        if feature.startswith("perm_entropy"):
            fac = math.factorial(d)
            return fac * max(1, round(window / fac)) + d
        if feature == "ptsr":
            return 2 * window
        if feature == "rai":
            return window + self.smoothing_tail() - 1
        return window + 1  # runs_z: window sign changes

    def feature_value(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        """The feature for ``ticker`` from bars ``<= as_of``; ``None`` when it
        can't be computed."""
        if lake is None:
            return None
        interval = Interval.parse(self.params["interval"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, interval, as_of, self._tail_bars()
        )
        if bars.empty:
            return None
        memo = self._lake_values(lake)
        key = (ticker, bars["timestamp"].iloc[-1])
        if memo is not None and key in memo:
            return memo[key]
        value = self._compute(bars)
        if memo is not None:
            memo[key] = value
        return value

    def _compute(self, bars: pd.DataFrame) -> float | None:
        feature = self.params["feature"]
        window = int(self.params["window"])
        d = int(self.params["d"])
        column = "volume" if feature == "perm_entropy_volume" else "close"
        arr = pd.to_numeric(bars[column], errors="coerce").to_numpy(dtype=float)
        if np.any(np.isnan(arr)):
            return None
        value = float("nan")
        if feature.startswith("perm_entropy"):
            value = float(rolling_permutation_entropy(arr, window, d)[-1])
        elif feature == "ptsr":
            if len(arr) >= window:
                # carry the latest valid window back at most ``window`` windows
                for end in range(len(arr), window - 1, -1):
                    value = self._window("ptsr", arr[end - window : end], lambda w: ptsr(w, d))
                    if not np.isnan(value):
                        break
        elif feature == "rai":
            n_windows = len(arr) - window + 1
            if n_windows >= 1:
                vals = [
                    self._window("rai", arr[s : s + window], relative_async_index)
                    for s in range(max(0, n_windows - self.smoothing_tail()), n_windows)
                ]
                com = float(self.params["rai_smooth_com"])
                value = float(pd.Series(vals).ewm(com=com).mean().iloc[-1]) if com > 0 else vals[-1]
        elif len(arr) > window:  # runs_z
            signs = np.sign(np.diff(arr[-(window + 1) :]))
            value = float(runs_test_z_score(signs))
        return None if not np.isfinite(value) else value

    def _window(self, kind: str, window: np.ndarray, fn: Any) -> float:
        key = (kind, window.tobytes())
        cached = self._memo.get(key)
        if cached is None:
            if len(self._memo) > _MEMO_MAX:
                self._memo.clear()
            cached = float(fn(window))
            self._memo[key] = cached
        return cached

    def _lake_values(self, lake: Any) -> dict | None:
        try:
            values = self._values.get(lake)
            if values is None:
                values = {}
                self._values[lake] = values
        except TypeError:
            return None
        return values

    def is_risk_off(self, ticker: str, as_of: Any, lake: Any) -> bool:
        value = self.feature_value(ticker, as_of, lake)
        if value is None:
            return False
        threshold = float(self.params["threshold"])
        if self.params["direction"] == "risk_off_above":
            return value > threshold
        return value < threshold

    # ---- Strategy Protocol ---------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        values = dict(self._inner.extract_features(ticker, as_of, lake).values)
        value = self.feature_value(ticker, as_of, lake)
        if value is not None:
            values["regime_feature"] = value
        values["regime_risk_off"] = 1.0 if self.is_risk_off(ticker, as_of, lake) else 0.0
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is not None:
            self._remember_lake(lake)
            if self.is_risk_off(ticker, as_of, lake):
                return None
        return self._inner.estimate_return(ticker, as_of, lake)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        lake = self._recall_lake()
        if lake is None:
            return self._inner.decide(my_picks, portfolio, prices, as_of)
        tickers = {t for _, t in my_picks} | {
            t for t, q in portfolio.positions.items() if abs(q) > 1e-12
        }
        off = {t for t in tickers if self.is_risk_off(t, as_of, lake)}
        picks = [(s, t) for s, t in my_picks if t not in off]
        orders = [
            o for o in self._inner.decide(picks, portfolio, prices, as_of) if o.ticker not in off
        ]
        for ticker in sorted(off):
            qty = portfolio.positions.get(ticker, 0.0)
            if abs(qty) > 1e-12:  # a short is covered as a long is sold (BE-14)
                token = "sell" if qty > 0 else "cover"
                orders.append(
                    Order(
                        client_id=f"{self.id}:{token}:{ticker}:{iso(as_of)}",
                        ticker=ticker,
                        side="sell" if qty > 0 else "buy",
                        quantity=abs(qty),
                        order_type="market",
                        strategy_id=self.id,
                        position_effect="close",
                    )
                )
        return orders
