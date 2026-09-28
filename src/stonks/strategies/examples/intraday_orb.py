"""Opening range breakout on minute bars (roadmap 21.3.1).

Rule, over the closed bars of today's regular session:

- the opening range is the high and the low of the bars that close within
  ``range_minutes`` of the open
- once the range is complete, the first close above the range high buys
- a close below the range low exits (the stop), and the strategy does not
  trade again that session
- flat ``exit_minutes_before_close`` before the close (see ``_intraday``)

Score while long: how far the close sits above the range high, floored at
a small positive number.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from stonks.core.params import ParameterSpec
from stonks.strategies.examples._intraday import (
    US_SESSION_MINUTES,
    IntradayStrategy,
    SessionBars,
    bars_for,
)


class OpeningRangeBreakout(IntradayStrategy):
    id = "intraday_orb"
    applicable_asset_classes = ("equity", "commodity")
    hypothesis = (
        "The first half hour sets the day's reference range while overnight "
        "news is priced. A close above that range shows buyers still arriving, "
        "and the order flow of late buyers, stops of short sellers and trend "
        "followers carries the move on for part of the day, so the long pays "
        "for the false breakouts it stops out of at the range low. Who loses: "
        "short sellers who faded the open and have to cover. Fails on quiet "
        "days with no follow-through, on news-free drifting sessions, and "
        "when costs are high against a small range."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = US_SESSION_MINUTES - 30
    required_history_bars = 30

    @classmethod
    def rule_spec(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec(
                name="range_minutes",
                kind="int",
                default=30,
                bounds=(5, 120),
                description="Minutes after the open that form the opening range.",
            ),
        ]

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        rng = int(p["range_minutes"])
        return {
            "label_horizon_bars": bars_for(US_SESSION_MINUTES - rng, p["interval"]),
            "required_history_bars": bars_for(rng, p["interval"]),
        }

    def _range(self, view: SessionBars) -> tuple[float, float] | None:
        rng = float(self.params["range_minutes"])
        if view.minutes_open < rng:
            return None  # the range is still forming
        in_range = view.elapsed <= rng
        if not in_range.any():
            return None
        high = view.bars["high"].to_numpy(dtype=float)[in_range]
        low = view.bars["low"].to_numpy(dtype=float)[in_range]
        hi, lo = np.nanmax(high), np.nanmin(low)
        if not (np.isfinite(hi) and np.isfinite(lo)):
            return None
        return float(hi), float(lo)

    def _score(self, view: SessionBars) -> float | None:
        levels = self._range(view)
        if levels is None:
            return None
        hi, lo = levels
        after = view.close[view.elapsed > float(self.params["range_minutes"])]
        above = np.flatnonzero(after > hi)
        if above.size == 0:
            return None
        entry = int(above[0])
        if np.any(after[entry:] < lo):
            return None  # stopped out, one trade a session
        return float(after[-1] / hi - 1.0)

    def _features(self, view: SessionBars) -> dict[str, Any]:
        levels = self._range(view)
        if levels is None:
            return {}
        return {"range_high": levels[0], "range_low": levels[1]}
