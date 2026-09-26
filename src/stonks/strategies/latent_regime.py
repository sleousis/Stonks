"""LatentRegimeFilter: gate any strategy on a Markov-switching volatility regime (BL-46).

Sources: Hamilton (Markov switching, filtered not smoothed probabilities),
Tsay, Dixon et al. (hidden Markov models for markets).

The filter fits a two-state (``k_regimes``) Markov-switching model to the
daily log returns of a reference market (``regime_ticker``, SPY by
default) and, on every bar, runs the Hamilton filter over the last
``filter_bars`` returns up to ``as_of``. When the filtered probability of
the most volatile state is above ``threshold`` the market is risk off,
and ``mode`` decides what happens:

- ``block_new_buys``: the inner strategy runs as usual but its buys are
  dropped. Sells pass.
- ``exit_all``: no picks, and every long is sold.

Point in time (P12): ``fit(dataset)`` estimates the model on the train
window only (at most its last ``fit_bars`` bars). A filter that was never
fitted (a hand-built production strategy) fits once, lazily, on the
``fit_bars`` bars up to the first ``as_of`` it is asked about, and keeps
that model. The filter itself only reads bars dated on or before
``as_of``. Too little history to judge means risk on.

The model is stored as plain numbers in ``regime_model.json`` next to the
wrapper's params, and the inner strategy's state under ``inner/``.
"""

from __future__ import annotations

import json
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.regimes import MIN_FIT_RETURNS, MarkovSwitchingRegime
from stonks.logging import get_logger
from stonks.strategies._common import LakeBarCaches, as_datetime, iso, sell_all_longs
from stonks.strategies._wrapping import InnerStrategyWrapper, inner_param_specs

_MODEL_FILE = "regime_model.json"
_MEMO_MAX = 50_000
#: Fewest returns the filter needs to judge a bar.
_MIN_FILTER_RETURNS = 20

_log = get_logger("stonks.strategies.latent_regime")


class LatentRegimeFilter(InnerStrategyWrapper):
    id = "latent_regime_filter"
    id_suffix = "latent_regime"
    hypothesis = (
        "Markets switch between a calm and a turbulent state, and the "
        "turbulent one is where trend and carry strategies lose most. A "
        "Markov-switching model spots the switch from returns alone, "
        "earlier than a fixed volatility threshold. Adds no alpha of its "
        "own and lags at sharp turns."
    )

    @classmethod
    def parameter_spec(cls):
        return [
            *inner_param_specs(),
            ParameterSpec(
                name="regime_ticker",
                kind="categorical",
                default="SPY.US",
                bounds=None,
                tunable=False,
                description="Reference market whose returns define the regime.",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=0.7,
                bounds=(0.5, 0.95),
                description="Risk off when P(high-volatility state) exceeds this.",
            ),
            ParameterSpec(
                name="mode",
                kind="categorical",
                default="block_new_buys",
                bounds=["block_new_buys", "exit_all"],
                tunable=False,
                description="On risk off: drop buys, or sell every long.",
            ),
            ParameterSpec(
                name="k_regimes",
                kind="int",
                default=2,
                bounds=(2, 3),
                tunable=False,
                description="Number of hidden states.",
            ),
            ParameterSpec(
                name="fit_bars",
                kind="int",
                default=1260,
                bounds=(100, 5000),
                tunable=False,
                description="Most recent bars the model is fitted on.",
            ),
            ParameterSpec(
                name="filter_bars",
                kind="int",
                default=252,
                bounds=(21, 2000),
                tunable=False,
                description="Returns the Hamilton filter runs over on each bar.",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=["1d", "1w"],
                tunable=False,
                description="Bar interval of the regime ticker.",
            ),
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self.model: MarkovSwitchingRegime | None = None
        self._bar_caches = LakeBarCaches()
        self._memo: weakref.WeakKeyDictionary[Any, dict[str, float | None]] = (
            weakref.WeakKeyDictionary()
        )

    def data_tickers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*super().data_tickers(), str(self.params["regime_ticker"])]))

    # ---- model ------------------------------------------------------------------

    def _interval(self) -> Interval:
        return Interval.parse(self.params["interval"])

    def _fit_on(self, closes: np.ndarray) -> bool:
        returns = np.diff(np.log(closes))
        returns = returns[np.isfinite(returns)]
        if len(returns) < MIN_FIT_RETURNS:
            return False
        self.model = MarkovSwitchingRegime(k=int(self.params["k_regimes"])).fit(returns)
        self._memo = weakref.WeakKeyDictionary()
        return True

    def fit(self, dataset: Any) -> None:
        super().fit(dataset)
        start, end = dataset.train_window
        cache = self._bar_caches.for_lake(dataset.lake)
        bars = cache.last_n_bars(
            str(self.params["regime_ticker"]),
            self._interval(),
            as_datetime(end).replace(hour=23, minute=59, second=59),
            int(self.params["fit_bars"]) + 1,
        )
        if bars is not None and not bars.empty:
            bars = bars[as_datetime(start) <= bars["timestamp"]]
        closes = np.empty(0) if bars is None or bars.empty else bars["close"].to_numpy(dtype=float)
        if not self._fit_on(closes):
            _log.warning(
                "latent_regime.fit.insufficient_data",
                ticker=self.params["regime_ticker"],
                bars=len(closes),
            )

    def _closes(self, lake: Any, as_of: Any, n: int) -> np.ndarray:
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            str(self.params["regime_ticker"]), self._interval(), as_of, n
        )
        if bars is None or bars.empty:
            return np.empty(0)
        return bars["close"].to_numpy(dtype=float)

    def high_vol_probability(self, as_of: Any, lake: Any) -> float | None:
        """Filtered ``P(high-volatility state)`` on ``as_of`` (``None``
        when there is too little history)."""
        try:
            memo = self._memo.setdefault(lake, {})
        except TypeError:
            memo = {}
        key = iso(as_datetime(as_of))
        if key in memo:
            return memo[key]
        if self.model is None:
            self._fit_on(self._closes(lake, as_of, int(self.params["fit_bars"]) + 1))
        value: float | None = None
        if self.model is not None:
            closes = self._closes(lake, as_of, int(self.params["filter_bars"]) + 1)
            returns = np.diff(np.log(closes)) if len(closes) > 1 else np.empty(0)
            if len(returns) >= _MIN_FILTER_RETURNS:
                value = self.model.high_vol_probability(returns)
        if len(memo) > _MEMO_MAX:
            memo.clear()
        memo[key] = value
        return value

    def is_risk_off(self, as_of: Any, lake: Any) -> bool:
        p = self.high_vol_probability(as_of, lake)
        return p is not None and p > float(self.params["threshold"])

    # ---- Strategy Protocol --------------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        values = dict(self._inner.extract_features(ticker, as_of, lake).values)
        if lake is not None:
            p = self.high_vol_probability(as_of, lake)
            if p is not None:
                values["regime_p_high_vol"] = p
            values["regime_risk_off"] = 1.0 if self.is_risk_off(as_of, lake) else 0.0
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return self._inner.estimate_return(ticker, as_of, lake)
        self._remember_lake(lake)
        if self.params["mode"] == "exit_all" and self.is_risk_off(as_of, lake):
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
        if lake is None or not self.is_risk_off(as_of, lake):
            return self._inner.decide(my_picks, portfolio, prices, as_of)
        if self.params["mode"] == "exit_all":
            return sell_all_longs(self.id, portfolio, as_of)
        orders = self._inner.decide(my_picks, portfolio, prices, as_of)
        return [o for o in orders if o.side != "buy"]

    # ---- persistence ----------------------------------------------------------------

    def fitted_state(self) -> dict[str, Any]:
        return {} if self.model is None else self.model.to_dict()

    def save(self, path: Path) -> None:
        super().save(path)
        if self.model is not None:
            (Path(path) / _MODEL_FILE).write_text(
                json.dumps(self.model.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
            )

    @classmethod
    def load(cls, path: Path) -> LatentRegimeFilter:
        instance = super().load(path)
        assert isinstance(instance, LatentRegimeFilter)
        model_file = Path(path) / _MODEL_FILE
        if model_file.is_file():
            data = json.loads(model_file.read_text(encoding="utf-8"))
            instance.model = MarkovSwitchingRegime.from_dict(data)
        return instance
