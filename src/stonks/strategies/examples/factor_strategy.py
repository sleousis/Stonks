"""FactorStrategy: trade any factor (roadmap 22.2).

The ``factor`` param names a library factor (``mom_12_1``, ``low_vol_60``,
``piotroski_f``, ...; see ``stonks factors list``) or holds a formula in the
expression language (``$close / Ref($close, 20) - 1``). On each decision:

1. **Score.** The factor's value for every universe name, known at the
   decision (:meth:`~stonks.factors.base.Factor.values_at`: a daily decision
   sees its own bar, an intraday one only closed days, fundamentals from the
   day after their filing).
2. **Orient.** Multiply by the factor's ``direction``, so higher is better
   (a ``-1`` factor such as ``low_vol_60`` holds its lowest values).
3. **Top slice.** Keep the best ``top_pct`` of the scored names (at least
   one). Each kept name's ``estimate_return`` is its percentile rank in
   ``(0, 1]``; the rest get ``None``.
4. **Rebalance** equal weight on the last session of each month in
   ``rebalance_months`` (every month by default), holding in between.

A formula has no stated hypothesis of its own: write one in the lab trial
before you trust it (P1).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.factors.base import Factor
from stonks.factors.expression import ExpressionError
from stonks.factors.registry import resolve_factor
from stonks.features.momentum import is_quarter_rebalance_day
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import (
    CrossSectionMemo,
    lake_universe,
    orders_from_constructor,
    parse_universe,
    session_cutoff,
)

_MONTHLY = ",".join(str(m) for m in range(1, 13))
_REBALANCE_MONTHS = [_MONTHLY, "3,6,9,12", "2,5,8,11", "1,4,7,10", "6,12", "12"]
_EPS = 1e-9


def select_top(values: Mapping[str, float], direction: int, top_pct: float) -> dict[str, float]:
    """The best ``top_pct`` of ``values`` (at least one) after orienting by
    ``direction``, each mapped to its percentile rank in ``(0, 1]`` among
    all scored names. Ties break by ticker."""
    if not values:
        return {}
    ordered = sorted(values, key=lambda t: (-direction * values[t], t))
    n = len(ordered)
    keep = max(1, math.ceil(top_pct * n - _EPS))
    return {t: (n - i) / n for i, t in enumerate(ordered[:keep])}


class FactorStrategy(BaseStrategy):
    id = "factor"
    title = "Factor portfolio"
    summary = "Holds the top slice of names ranked by a factor you pick, rebalanced each month."
    hypothesis = (
        "The chosen factor ranks next-month returns across the universe, for the "
        "reason its own hypothesis states (see stonks factors show). The top slice, "
        "rebalanced monthly, earns that premium net of costs. Fails when the "
        "factor's premium is arbitraged away or its regime turns."
    )
    alpha_family = "data_driven"
    premise = "none"
    label_horizon_bars = 21
    required_history_bars = 253
    applicable_asset_classes = ("equity", "crypto", "commodity", "bond")

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        try:
            self._factor: Factor = resolve_factor(str(self.params["factor"]))
        except (ExpressionError, ValueError) as exc:
            raise ValueError(f"factor {self.params['factor']!r}: {exc}") from None
        if not 0.0 < float(self.params["top_pct"]) <= 1.0:
            raise ValueError("top_pct must be in (0, 1]")
        self.applicable_asset_classes = tuple(self._factor.asset_classes)  # type: ignore[assignment]
        self._memo = CrossSectionMemo()

    def param_metadata(self) -> dict[str, int]:
        try:
            factor = resolve_factor(str(self.params["factor"]))
        except (ExpressionError, ValueError):
            return {}  # __init__ reports the bad factor
        return {"required_history_bars": factor.lookback_bars + 1}

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="factor",
                kind="categorical",
                default="mom_12_1",
                bounds=None,
                tunable=False,
                description="A library factor id or a formula in the expression language.",
            ),
            ParameterSpec(
                name="top_pct",
                kind="float",
                default=0.2,
                bounds=(0.05, 0.5),
                description="Fraction of the scored universe held.",
            ),
            ParameterSpec(
                name="rebalance_months",
                kind="categorical",
                default=_MONTHLY,
                bounds=_REBALANCE_MONTHS,
                tunable=False,
                description="Months whose last session rebalances the book.",
            ),
            ParameterSpec(
                name="universe",
                kind="categorical",
                default="",
                bounds=None,
                tunable=False,
                description="Comma-separated tickers ranked together; empty = the lake's.",
            ),
        ]

    @property
    def factor(self) -> Factor:
        return self._factor

    # ---- signals -----------------------------------------------------------------

    def score_universe(self, tickers: Sequence[str], as_of: Any, lake: Any) -> dict[str, float]:
        """``{ticker: percentile}`` for the names held from ``tickers``."""
        values = self._factor.values_at(lake, list(dict.fromkeys(tickers)), as_of)
        return select_top(values, self._factor.direction, float(self.params["top_pct"]))

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return None
        day, _ = session_cutoff(as_of)
        held = self._memo.get(
            lake, day, lambda: self.score_universe(self._universe(lake), as_of, lake)
        )
        return held.get(ticker)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        months = [int(m) for m in str(self.params["rebalance_months"]).split(",")]
        if not is_quarter_rebalance_day(day, months):
            return []
        signals = {t: 1.0 for r, t in my_picks if r > 0}
        inp = ConstructionInput(
            signals={self.id: signals}, portfolio=portfolio, prices=prices, as_of=day
        )
        constructor = get_constructor("equal_weight_top_n", n=max(1, len(signals)))
        return orders_from_constructor(constructor, inp, strategy_id=self.id)

    # ---- internals ---------------------------------------------------------------

    def _universe(self, lake: Any) -> list[str]:
        explicit = parse_universe(str(self.params["universe"]))
        if explicit:
            return explicit
        return self._memo.universe(lake, lambda: lake_universe(lake, self.applicable_asset_classes))
