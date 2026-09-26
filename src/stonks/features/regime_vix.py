"""VIX term-structure regime condition (BL-46; Sinclair, Gatheral).

In calm markets the VIX futures curve slopes up: 30-day implied volatility
(``vix_spot``) sits below 3-month implied volatility (``vix_3m``). In a
panic it inverts (backwardation), because traders pay most for protection
right now. ``vix_term_structure`` triggers (risk off) when
``vix_spot / vix_3m > threshold`` (1.0 by default, the inversion).

Both series live in ``macro_indicators`` (country ``USA``), filled by
``stonks ingest macro --source yahoo --countries USA --indicators
vix_spot,vix_3m``. Each leg is its latest close dated on or before
``as_of`` (plus ``publication_lag_days``, 0 by default: a daily close is
known when the day ends, like the bar a strategy decides on). A leg older
than ``max_staleness_days`` means unknown.

Registered with the condition registry just by living in a
``features/regime_*.py`` module.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar

import numpy as np
from pydantic import Field

from stonks.features.regime_conditions import ConditionContext, RegimeCondition, _day

__all__ = ["VixTermStructureCondition"]


class VixTermStructureCondition(RegimeCondition):
    """Spot VIX over 3-month VIX above ``threshold`` (an inverted curve)."""

    kind: ClassVar[str] = "vix_term_structure"

    country_iso: str = "USA"
    spot_indicator: str = "vix_spot"
    term_indicator: str = "vix_3m"
    threshold: float = Field(default=1.0, gt=0.5, lt=2.0)
    publication_lag_days: int = Field(default=0, ge=0, le=30)
    max_staleness_days: int = Field(default=5, ge=1, le=60)

    def _series(self, indicator: str, ctx: ConditionContext) -> tuple[np.ndarray, np.ndarray]:
        key = ("vix", self.country_iso, indicator)
        cached = ctx.memo.get(key)
        if cached is None:
            df = ctx.lake.get_macro_series(self.country_iso, indicator, stamped_at="period_end")
            df = df[df["value"].notna()]
            days = np.array(
                [np.datetime64(d, "D") for d in df["observation_date"]], dtype="datetime64[D]"
            )
            cached = (days, df["value"].to_numpy(dtype=float))
            ctx.memo[key] = cached
        return cached

    def _latest(self, indicator: str, day: date, ctx: ConditionContext) -> float | None:
        days, values = self._series(indicator, ctx)
        known_by = np.datetime64(day - timedelta(days=self.publication_lag_days), "D")
        i = int(np.searchsorted(days, known_by, "right"))
        if i == 0:
            return None
        age = int((known_by - days[i - 1]).astype(int))
        if age > self.max_staleness_days:
            return None
        return float(values[i - 1])

    def triggered(self, as_of: Any, ctx: ConditionContext) -> bool | None:
        day = _day(as_of)
        spot = self._latest(self.spot_indicator, day, ctx)
        term = self._latest(self.term_indicator, day, ctx)
        if spot is None or term is None or not term > 0:
            return None
        return spot / term > self.threshold
