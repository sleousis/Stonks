"""ForecastSignal: trade a forecasting model's view (roadmap 23.11).

Rules, per ticker, on adjusted daily bars:

1. Hand the last ``context_bars`` bars complete at the decision to the
   forecaster named by ``model`` (any name in the forecaster registry:
   the ``theta`` baseline by default, or a pretrained model such as
   ``chronos_bolt``, ``chronos_2``, ``timesfm_2_5`` or ``kronos_small``).
2. The signal is the forecast log return ``horizon`` bars ahead (the
   median close over the last close). A ticker is a pick when that return
   is above ``min_return`` and the model's P(up) is at least
   ``min_prob_up``.
3. Long only. ``decide`` sells held names that are no longer picks and
   spreads ``allocation`` of the cash equally over new picks.

The model is fixed per run, never tuned: each model is its own hypothesis
and its own trial family. A pretrained model is judged only on windows
after its pretraining cutoff (``forecast_models`` tells the lab which
model runs, and the preflight refuses earlier windows), and the lab adds
the ``forecast_skill`` survival test, so the model must beat the random
walk, ETS and Theta before the backtest counts. When the model's optional
group is not installed the strategy gives no signal and logs why.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import numpy as np

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass, Features, Order, Portfolio
from stonks.features.forecasters import Forecast, Forecaster, build_forecaster, forecaster_names
from stonks.features.forecasters._pretrained import ModelUnavailableError
from stonks.logging import get_logger
from stonks.strategies._common import LakeBarCaches
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import is_fresh, session_cutoff

_log = get_logger("stonks.strategies.forecast_signal")


class ForecastSignal(BaseStrategy):
    id = "forecast_signal"
    summary = "Buys the stocks a forecasting model expects to rise over the next few days."
    hypothesis = (
        "A forecasting model (a pretrained foundation model or a statistical "
        "baseline) finds short-horizon structure in a ticker's own prices that "
        "the random walk misses. We are paid by traders who react late to the "
        "patterns the model learned. Fails when the model only fits noise, "
        "which the forecast_skill test checks, and when costs eat a small edge."
    )
    alpha_family = "data_driven"
    premise = "none"
    label_horizon_bars = 5
    required_history_bars = 128
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity", "crypto", "commodity")

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()
        self._model: Forecaster | None = None
        self._unavailable = False

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="model",
                kind="categorical",
                default="theta",
                bounds=forecaster_names(),
                tunable=False,
                description="The forecaster to run (see docs/forecasting.md).",
            ),
            ParameterSpec(
                name="horizon",
                kind="int",
                default=5,
                bounds=(1, 63),
                tunable=False,
                description="Bars ahead the model forecasts.",
            ),
            ParameterSpec(
                name="context_bars",
                kind="int",
                default=128,
                bounds=(32, 2048),
                tunable=False,
                description="Bars of history handed to the model.",
            ),
            ParameterSpec(
                name="min_return",
                kind="float",
                default=0.0,
                bounds=(0.0, 0.05),
                description="Smallest forecast log return that makes a pick.",
            ),
            ParameterSpec(
                name="min_prob_up",
                kind="float",
                default=0.5,
                bounds=(0.5, 0.95),
                description="Smallest model P(up) that makes a pick.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash spread over new picks.",
            ),
        ]

    @classmethod
    def forecast_models(cls, params: Mapping[str, Any]) -> tuple[str, ...]:
        """The forecaster a run with ``params`` uses (the preflight's cutoff
        rule reads this)."""
        return (str(params.get("model") or "theta"),)

    def param_metadata(self) -> dict[str, int]:
        return {
            "required_history_bars": int(self.params["context_bars"]),
            "label_horizon_bars": int(self.params["horizon"]),
        }

    @property
    def forecast_horizon(self) -> int:
        return int(self.params["horizon"])

    @property
    def forecast_context_bars(self) -> int:
        return int(self.params["context_bars"])

    def forecaster(self) -> Forecaster:
        """The model, through the forecaster seam (built once)."""
        if self._model is None:
            self._model = build_forecaster(str(self.params["model"]))
        return self._model

    def __getstate__(self) -> dict[str, Any]:
        # a loaded model never crosses to a worker process: each loads its own
        state = dict(self.__dict__)
        state["_model"] = None
        return state

    # ---- evaluation --------------------------------------------------------------

    def _forecast(self, ticker: str, as_of: Any, lake: Any) -> Forecast | None:
        if lake is None or self._unavailable:
            return None
        _, cutoff = session_cutoff(as_of)
        cache = self._bar_caches.for_lake(lake)
        if not is_fresh(cache, ticker, cutoff):
            return None
        n = int(self.params["context_bars"])
        bars = cache.last_n_bars(ticker, Interval.DAY_1, cutoff, n)
        if len(bars) < n:
            return None
        closes = np.asarray(bars["close"], dtype=float)
        if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
            return None
        try:
            return self.forecaster().predict(bars.reset_index(drop=True), self.forecast_horizon)
        except ModelUnavailableError as exc:
            self._unavailable = True
            _log.warning(
                "forecast_signal.model_unavailable", model=self.params["model"], error=str(exc)
            )
            return None

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        f = self._forecast(ticker, as_of, lake)
        if f is None:
            return Features(values={})
        return Features(values={"forecast_return": f.log_return(), "prob_up": f.prob_up()})

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        f = self._forecast(ticker, as_of, lake)
        if f is None:
            return None
        r = f.log_return()
        if not math.isfinite(r) or r <= float(self.params["min_return"]):
            return None
        return r if f.prob_up() >= float(self.params["min_prob_up"]) else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        stamp = day.isoformat()
        picked = {t for _, t in my_picks}
        orders: list[Order] = []
        for ticker, qty in list(portfolio.positions.items()):
            if qty > 0 and ticker not in picked:
                orders.append(
                    Order(
                        client_id=f"{self.id}:sell:{ticker}:{stamp}",
                        ticker=ticker,
                        side="sell",
                        quantity=qty,
                        order_type="market",
                        strategy_id=self.id,
                    )
                )
        new = [
            t
            for _, t in sorted(my_picks, reverse=True)
            if portfolio.positions.get(t, 0.0) <= 0 and (prices.get(t) or 0) > 0
        ]
        if not new or portfolio.cash <= 0:
            return orders
        budget = portfolio.cash * float(self.params["allocation"]) / len(new)
        for ticker in new:
            qty = budget / float(prices[ticker])
            if qty > 0:
                orders.append(
                    Order(
                        client_id=f"{self.id}:buy:{ticker}:{stamp}",
                        ticker=ticker,
                        side="buy",
                        quantity=qty,
                        order_type="market",
                        strategy_id=self.id,
                    )
                )
        return orders
