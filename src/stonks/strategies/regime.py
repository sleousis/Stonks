"""RegimeFilter: a composite k-of-n regime gate around any strategy (BL-42).

Sources: Clenow (index 200-day average), Faber (10-month average), Elder
(Triple Screen), Davey (bull/bear filter), Kindleberger and Lefevre (credit
and panic signs).

The filter holds ``n`` conditions from :mod:`stonks.features.regime_conditions`
(macro, price trend, realised volatility, yield curve, higher-timeframe
trend, and any condition a later module registers). On each bar it counts
how many trigger. Risk off when at least ``k`` do. Then ``mode`` decides:

- ``block_new_buys``: the inner strategy runs as usual but its buys are
  dropped. Sells pass, and held names stay in the ranking, so the inner
  does not dump them just because the gate is shut.
- ``exit_all``: no picks and every long is sold.
- ``scale``: buy quantities are multiplied by the share of conditions that
  did not trigger (all triggered means no buys).

Sells always pass. A condition that can't be judged (no data, too little
history, stale data) counts as not triggered, or as triggered with
``when_unknown="trigger"``. Every condition reads only data dated on or
before ``as_of``.

The wrapper plumbing (``inner_class_path`` + ``inner_params``, the inner's
state saved under ``inner/``) is shared with the other wrappers in
:mod:`stonks.strategies._wrapping`. The tunable knobs (``trend_sma``,
``trend_hysteresis``, ``vol_window``, ``vol_pct``, ``htf_ema``) fill the
matching field of every condition whose spec leaves it out.
"""

from __future__ import annotations

import copy
import dataclasses
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.regime_conditions import ConditionContext, RegimeCondition, build_condition
from stonks.strategies._common import (
    LakeBarCaches,
    as_datetime,
    close_all_positions,
    closing_orders,
    iso,
)
from stonks.strategies._wrapping import InnerStrategyWrapper, inner_param_specs

DEFAULT_CONDITIONS: list[dict[str, Any]] = [{"kind": "price_trend", "ticker": "SPY.US"}]


@dataclass
class _LakeState:
    ctx: ConditionContext
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)


class RegimeFilter(InnerStrategyWrapper):
    id = "regime_filter"
    id_suffix = "regime"
    hypothesis = (
        "Trend and momentum strategies lose most in bear markets, high "
        "volatility and credit stress. Standing aside when several such "
        "signs agree cuts drawdowns more than it cuts returns. Adds no alpha "
        "of its own and costs some upside after fast V-shaped recoveries."
    )

    @classmethod
    def parameter_spec(cls):
        return [
            *inner_param_specs(),
            ParameterSpec(
                name="conditions",
                kind="categorical",
                default=DEFAULT_CONDITIONS,
                bounds=None,
                tunable=False,
                description="Regime conditions: a list of {'kind': ..., **fields}.",
            ),
            ParameterSpec(
                name="k",
                kind="int",
                default=1,
                bounds=(1, 10),
                description="Risk off when at least k conditions trigger.",
            ),
            ParameterSpec(
                name="mode",
                kind="categorical",
                default="block_new_buys",
                bounds=["block_new_buys", "exit_all", "scale"],
                tunable=False,
                description="On risk off: drop buys, sell every long, or scale buys "
                "by the share of conditions not triggered.",
            ),
            ParameterSpec(
                name="when_unknown",
                kind="categorical",
                default="ignore",
                bounds=["ignore", "trigger"],
                tunable=False,
                description="How a condition that can't be judged counts.",
            ),
            ParameterSpec(
                name="trend_sma",
                kind="int",
                default=200,
                bounds=(50, 300),
                description="Default SMA length of price_trend conditions.",
            ),
            ParameterSpec(
                name="trend_hysteresis",
                kind="float",
                default=0.0,
                bounds=(0.0, 0.03),
                description="Default hysteresis band of price_trend conditions.",
            ),
            ParameterSpec(
                name="vol_window",
                kind="int",
                default=21,
                bounds=(10, 63),
                description="Default window of realized_vol conditions.",
            ),
            ParameterSpec(
                name="vol_pct",
                kind="float",
                default=0.8,
                bounds=(0.5, 0.95),
                description="Default quantile of realized_vol conditions.",
            ),
            ParameterSpec(
                name="htf_ema",
                kind="int",
                default=26,
                bounds=(10, 52),
                description="Default EMA length of higher_timeframe conditions.",
            ),
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        specs = self.params["conditions"]
        if not isinstance(specs, list | tuple) or not specs:
            raise ValueError("conditions must be a list of at least one condition")
        self.params["conditions"] = copy.deepcopy(list(specs))
        self.conditions: list[RegimeCondition] = [
            build_condition(spec, self.params) for spec in self.params["conditions"]
        ]
        # RS-29: k is tunable up to 10 whatever the conditions, so clamp it
        # rather than fail the trial (k = n means "all conditions agree").
        self.params["k"] = min(int(self.params["k"]), len(self.conditions))
        self._bar_caches = LakeBarCaches()
        self._lakes: weakref.WeakKeyDictionary[Any, _LakeState] = weakref.WeakKeyDictionary()

    def data_tickers(self) -> tuple[str, ...]:
        """The inner strategy's data tickers plus every ticker a condition
        reads (any string field whose name ends in ``ticker``)."""
        own = [
            value
            for cond in self.conditions
            for name, value in cond.spec().items()
            if name.endswith("ticker") and isinstance(value, str) and value
        ]
        return tuple(dict.fromkeys([*super().data_tickers(), *own]))

    # ---- regime -------------------------------------------------------------------

    def _state(self, lake: Any) -> _LakeState:
        def fresh() -> _LakeState:
            return _LakeState(ConditionContext(lake, bars=self._bar_caches.for_lake(lake)))

        try:
            state = self._lakes.get(lake)
            if state is None:
                state = fresh()
                self._lakes[lake] = state
        except TypeError:
            return fresh()
        return state

    def triggered_count(self, as_of: Any, lake: Any) -> tuple[int, int]:
        """(conditions triggered, conditions) on ``as_of``."""
        state = self._state(lake)
        key = iso(as_datetime(as_of))
        cached = state.counts.get(key)
        if cached is None:
            unknown_counts = self.params["when_unknown"] == "trigger"
            hits = 0
            for cond in self.conditions:
                result = cond.triggered(as_of, state.ctx)
                hits += bool(result) if result is not None else unknown_counts
            cached = (hits, len(self.conditions))
            state.counts[key] = cached
        return cached

    def is_risk_off(self, as_of: Any, lake: Any) -> bool:
        return self.triggered_count(as_of, lake)[0] >= int(self.params["k"])

    # ---- Strategy Protocol ----------------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        values = dict(self._inner.extract_features(ticker, as_of, lake).values)
        if lake is not None:
            hits, _n = self.triggered_count(as_of, lake)
            values["regime_triggered"] = float(hits)
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
        mode = self.params["mode"]
        if mode == "exit_all":
            return close_all_positions(self.id, portfolio, as_of)
        orders = self._inner.decide(my_picks, portfolio, prices, as_of)
        if mode == "block_new_buys":
            if self.supports_short:  # covers go through, new shorts do not
                return closing_orders(orders, portfolio)
            return [o for o in orders if o.side != "buy"]
        hits, n = self.triggered_count(as_of, lake)
        share = (n - hits) / n
        if self.supports_short:
            return _scale_opening(orders, portfolio, share)
        out: list[Order] = []
        for order in orders:
            if order.side != "buy":
                out.append(order)
            elif share > 0:
                out.append(dataclasses.replace(order, quantity=order.quantity * share))
        return out


def _scale_opening(orders: Sequence[Order], portfolio: Portfolio, share: float) -> list[Order]:
    """Closes as they are, opening legs (either side) scaled by ``share``."""
    from stonks.execution.orders import classify_all

    out: list[Order] = []
    for leg in classify_all(orders, portfolio.positions):
        if leg.position_effect == "close":
            out.append(leg)
        elif share > 0:
            out.append(dataclasses.replace(leg, quantity=leg.quantity * share))
    return out
