"""ForecastBlend: Carver's trend rules combined with forecast weights
estimated net of costs, per instrument (roadmap 22.7; Carver, *Systematic
Trading* and *Advanced Futures Trading Strategies*; pysystemtrade).

Rules, per ticker, on adjusted daily closes:

1. Rules: EWMAC at fast spans 2 to 64 (slow = 4x fast) and scaled
   time-series momentum at 125 and 250 sessions (``rules``). Each raw
   forecast is scaled (Carver's published scalars, or estimated) and capped
   at +/-20.
2. Fit, once per ``refit`` period (a year by default, like pysystemtrade's
   expanding window). The fit uses only bars dated **before** the first
   bar of the current period (principle P12), so a period trades with
   weights it could have known on its first day:
   - each rule's turnover and its yearly cost in Sharpe units, from the
     instrument's one-way trade cost (half-spread plus fee of its asset
     class) times ``cost_multiplier``. The cost model comes through the
     seam in :mod:`stonks.strategies.costs`: the lab, the backtest and the
     tick bind theirs (``[backtest.costs]`` by default), and without one
     ``CostModelSettings.realistic()`` applies;
   - the speed limit: a rule that costs more than ``max_cost_sr`` (0.13
     SR a year) is dropped for this instrument (principle P19);
   - weights for the rest from the ``weight_method`` estimator
     (``handcraft``, ``bootstrap`` or ``equal``) on the rules' net returns;
   - the FDM from the kept rules' forecast correlation (``fdm_mode``).
   With under a year of training rows the fit is a warm-up: every rule at
   equal weight with the fixed FDM.
3. Combine: ``cap(fdm * sum w_j f_j)`` over the kept rules. No forecast
   when every rule is too costly.
4. Sizing, long only by default, is the shared ``vol_target`` path of
   :mod:`stonks.strategies.examples._forecast_trend`.

:meth:`ForecastBlend.forecast_weight_fits` returns the latest fit per
ticker, which the backtest tear sheet shows (each rule's weight, cost and
whether it was dropped).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pandas as pd

from stonks.backtest.costs import CostModelSettings
from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass
from stonks.features.forecast import (
    DEFAULT_VOL_SPAN,
    EWMAC_FORECAST_SCALARS,
    TSMOM_SCALED_SCALAR,
    cap_forecast,
    combine_forecasts,
    ewmac_raw,
    tsmom_raw,
)
from stonks.features.forecast_weights import (
    DEFAULT_MAX_COST_SR,
    ForecastWeightFit,
    fit_forecast_weights,
    get_weight_estimator,
    weight_estimator_names,
)
from stonks.features.volatility import periods_per_year
from stonks.strategies.costs import trade_costs_or_default
from stonks.strategies.examples._forecast_trend import (
    ForecastTrendStrategy,
    forecast_specs,
    sizing_specs,
)

RULE_SETS = [
    "ewmac2,ewmac4,ewmac8,ewmac16,ewmac32,ewmac64,tsmom125,tsmom250",
    "ewmac2,ewmac4,ewmac8,ewmac16,ewmac32,ewmac64",
    "ewmac8,ewmac16,ewmac32,ewmac64,tsmom250",
    "ewmac16,ewmac64,tsmom250",
]
REFIT_PERIODS = ["year", "quarter", "month"]
#: Bars of trailing history the live forecast is computed on, as a
#: multiple of the slowest EWMAC span (the seed then weighs under 0.1%).
WINDOW_MULTIPLE = 16
_BPS = 10_000.0
#: The bound cost model, next to ``params.json`` in a saved strategy.
COSTS_FILE = "costs.json"

RawRule = Callable[[pd.Series, int, int], pd.Series]


def _ewmac(closes: pd.Series, fast: int, vol_span: int) -> pd.Series:
    return ewmac_raw(closes, fast, vol_span=vol_span)


def _tsmom(closes: pd.Series, lookback: int, vol_span: int) -> pd.Series:
    return tsmom_raw(closes, lookback, "scaled", vol_span=vol_span)


#: Rule families: raw forecast, fixed scalar and bars needed, by prefix.
_FAMILIES: dict[str, tuple[RawRule, Callable[[int], float], Callable[[int], int]]] = {
    "ewmac": (_ewmac, lambda n: EWMAC_FORECAST_SCALARS[n], lambda n: 4 * n),
    "tsmom": (_tsmom, lambda n: TSMOM_SCALED_SCALAR, lambda n: n + 1),
}


def parse_rule(name: str) -> tuple[str, int]:
    """``"ewmac16"`` -> ``("ewmac", 16)``."""
    for prefix in _FAMILIES:
        if name.startswith(prefix) and name[len(prefix) :].isdigit():
            return prefix, int(name[len(prefix) :])
    raise ValueError(f"unknown forecast rule {name!r}")


def period_start(ts: pd.Timestamp, refit: str) -> pd.Timestamp:
    """Midnight of the first day of ``ts``'s year, quarter or month."""
    day = ts.normalize()
    if refit == "year":
        return day.replace(month=1, day=1)
    if refit == "quarter":
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    if refit == "month":
        return day.replace(day=1)
    raise ValueError(f"refit must be one of {REFIT_PERIODS}, got {refit!r}")


class ForecastBlend(ForecastTrendStrategy):
    id = "forecast_blend"
    hypothesis = (
        "Trend rules at several speeds each predict returns (Carver; Hurst, "
        "Ooi & Pedersen), but the fast ones trade so often that costs eat "
        "their edge on expensive instruments. Weighting rules by how they "
        "diversify, net of what they cost to trade on each instrument, and "
        "dropping rules above Carver's speed limit keeps the trend premium "
        "and pays less of it away. Fails in choppy markets and at sharp "
        "reversals, like any trend rule, and when costs rise after a fit."
    )
    label_horizon_bars = 21
    required_history_bars = 256

    def __init__(self, params: Any, *, costs: CostModelSettings | None = None) -> None:
        super().__init__(params)
        self._fit_cache: dict[str, tuple[tuple[Any, ...], ForecastWeightFit]] = {}
        self._fits: dict[str, ForecastWeightFit] = {}
        self._costs = costs

    # ---- costs (the CostAware seam) ------------------------------------------------

    @property
    def costs(self) -> CostModelSettings | None:
        """The cost model the caller bound; ``None`` means the defaults."""
        return self._costs

    def bind_costs(self, costs: CostModelSettings) -> None:
        """Fit with ``costs`` from now on (earlier fits and the forecasts
        cached from them are dropped)."""
        if costs != self._costs:
            self._costs = costs
            self._fit_cache.clear()
            self._days.clear()

    def save(self, path: Path) -> None:
        super().save(path)
        if self._costs is not None:
            (Path(path) / COSTS_FILE).write_text(self._costs.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> ForecastBlend:
        params = json.loads((Path(path) / "params.json").read_text())
        stored = Path(path) / COSTS_FILE
        costs = (
            CostModelSettings.model_validate_json(stored.read_text()) if stored.is_file() else None
        )
        return cls(params, costs=costs)

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="rules",
                kind="categorical",
                default=RULE_SETS[0],
                bounds=RULE_SETS,
                description="Trend rules to combine: ewmacN (fast span N) and tsmomN.",
            ),
            ParameterSpec(
                name="weight_method",
                kind="categorical",
                default="handcraft",
                bounds=weight_estimator_names(),
                tunable=False,
                description="Forecast weight estimator: handcraft, bootstrap or equal.",
            ),
            ParameterSpec(
                name="max_cost_sr",
                kind="float",
                default=DEFAULT_MAX_COST_SR,
                bounds=(0.01, 1.0),
                tunable=False,
                description="Speed limit: drop a rule that costs more Sharpe a year than this.",
            ),
            ParameterSpec(
                name="cost_multiplier",
                kind="float",
                default=1.0,
                bounds=(0.0, 100.0),
                tunable=False,
                description="Multiplier on the asset class's one-way trade cost.",
            ),
            ParameterSpec(
                name="refit",
                kind="categorical",
                default="year",
                bounds=REFIT_PERIODS,
                tunable=False,
                description="Refit the weights at the start of each year, quarter or month.",
            ),
            ParameterSpec(
                name="vol_span",
                kind="int",
                default=DEFAULT_VOL_SPAN,
                bounds=(10, 100),
                tunable=False,
                description="EWMA span of the sigma inside each rule.",
            ),
            *forecast_specs(fdm_mode_default="estimate"),
            *sizing_specs(),
        ]

    # ---- rules ---------------------------------------------------------------------

    def rules(self) -> list[str]:
        names = [r.strip() for r in str(self.params["rules"]).split(",") if r.strip()]
        for name in names:
            parse_rule(name)
        return names

    def fixed_scalars(self) -> dict[str, float]:
        out = {}
        for name in self.rules():
            family, n = parse_rule(name)
            out[name] = _FAMILIES[family][1](n)
        return out

    def raw_rule_forecasts(self, closes: pd.Series) -> pd.DataFrame:
        """Each rule's unscaled forecast, one column per rule."""
        vol_span = int(self.params["vol_span"])
        cols = {}
        for name in self.rules():
            family, n = parse_rule(name)
            cols[name] = _FAMILIES[family][0](closes, n, vol_span)
        return pd.DataFrame(cols, index=closes.index)

    def window_bars(self) -> int:
        """Trailing bars the live forecast is computed on."""
        slowest = max(
            (WINDOW_MULTIPLE * n if f == "ewmac" else n + 1 + 4 * int(self.params["vol_span"]))
            for f, n in map(parse_rule, self.rules())
        )
        return max(slowest, self._min_bars())

    def _history_bars(self) -> int | None:
        return None  # the fit reads the whole history before its period

    def _min_bars(self) -> int:
        return max(_FAMILIES[f][2](n) for f, n in map(parse_rule, self.rules()))

    # ---- fit -----------------------------------------------------------------------

    def forecast_weight_fits(self) -> dict[str, ForecastWeightFit]:
        """The latest fit of each ticker evaluated (for reports)."""
        return dict(self._fits)

    def _fit(self, ticker: str, train: pd.DataFrame, asset_class: str) -> ForecastWeightFit:
        closes = pd.Series(
            train["close"].astype(float).to_numpy(), index=pd.to_datetime(train["timestamp"])
        )
        key = (
            len(closes),
            closes.index[0] if len(closes) else None,
            closes.index[-1] if len(closes) else None,
            float(closes.iloc[-1]) if len(closes) else None,
        )
        cached = self._fit_cache.get(ticker)
        if cached is not None and cached[0] == key:
            return cached[1]
        klass = cast(AssetClass, asset_class)
        cost = (
            trade_costs_or_default(self._costs).one_way_cost_bps(klass)
            / _BPS
            * float(self.params["cost_multiplier"])
        )
        fit = fit_forecast_weights(
            self.raw_rule_forecasts(closes),
            closes,
            fixed_scalars=self.fixed_scalars(),
            cost=cost,
            estimator=get_weight_estimator(str(self.params["weight_method"])),
            max_cost_sr=float(self.params["max_cost_sr"]),
            periods_per_year=periods_per_year(klass),
            scalar_mode="estimate" if self.params["scalar_mode"] == "estimate" else "fixed",
            fdm_mode="estimate" if self.params["fdm_mode"] == "estimate" else "fixed",
            fdm_fallback=float(self.params["fdm"]),
        )
        self._fit_cache[ticker] = (key, fit)
        return fit

    # ---- evaluation ----------------------------------------------------------------

    def _ticker_forecast(self, ticker: str, bars: pd.DataFrame, asset_class: str) -> float | None:
        stamps = pd.DatetimeIndex(pd.to_datetime(bars["timestamp"]))
        start = period_start(cast(pd.Timestamp, stamps[-1]), str(self.params["refit"]))
        fit = self._fit(ticker, bars.iloc[: int((stamps < start).sum())], asset_class)
        self._fits[ticker] = fit
        weights = fit.weights
        if not weights:
            return None
        window = bars.iloc[-self.window_bars() :]
        raw = self.raw_rule_forecasts(window["close"].astype(float).reset_index(drop=True))
        scaled = pd.DataFrame({n: cap_forecast(raw[n] * fit.scalars[n]) for n in weights})
        value = combine_forecasts(scaled, weights, fdm=fit.fdm).iloc[-1]
        return None if pd.isna(value) else float(value)

    def _signed_forecast(self, bars: pd.DataFrame, asset_class: str) -> float | None:
        raise NotImplementedError("ForecastBlend forecasts per ticker (_ticker_forecast)")
