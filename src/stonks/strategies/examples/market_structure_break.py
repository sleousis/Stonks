"""MarketStructureBreakStrategy — long a break of hierarchical market structure.

Built on the hierarchical extremes of neurotrader888's MIT-licensed
``market-structure`` repository (https://github.com/neurotrader888/market-structure,
Copyright (c) neurotrader888); see :mod:`stonks.features.extremes` for the
re-implementation of the ATR directional change and the level promotion.

Rule (counts in bars):

- Swing points come from an ATR directional change (simple-mean ATR over
  ``atr_lookback`` bars); level ``L + 1`` holds the level-``L`` extremes
  that beat their same-type neighbours on both sides. Every extreme is
  usable only from its confirmation bar on.
- Go long when the close is above the last confirmed level-``level`` high
  (structure broken upward); go flat when the close is below the last
  confirmed level-``level`` low; otherwise keep the previous position.

The position is rebuilt on every call by replaying a fixed window of the
latest bars from scratch, so a fresh production instance matches a
backtest that stepped through every bar.

Deliberate deviations from the original research code:

- The original repository ships the extreme detector only (its demo plots
  the level-3 low); the breakout rule on top of it is ours, and long-only:
  a break below the last low goes flat instead of short.
- History is limited to the last ``max(3000, 20 * atr_lookback)`` bars;
  structure is rebuilt from the start of that window.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from stonks.core.params import ParameterSpec
from stonks.features.extremes import hierarchical_level_prices
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs

LEVELS = 5


def _positions_from_levels(close: np.ndarray, hi: np.ndarray, lo: np.ndarray) -> np.ndarray:
    pos = np.zeros(len(close))
    state = 0.0
    for i in range(len(close)):
        if close[i] > hi[i]:  # NaN compares False: no level yet, no signal
            state = 1.0
        elif close[i] < lo[i]:
            state = 0.0
        pos[i] = state
    return pos


def market_structure_positions(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, atr_lookback: int, level: int
) -> np.ndarray:
    """Per-bar long (1.0) / flat (0.0) position as known at each close."""
    close = np.asarray(close, dtype=float)
    hi, lo = hierarchical_level_prices(high, low, close, atr_lookback, level, LEVELS)
    return _positions_from_levels(close, hi, lo)


class MarketStructureBreakStrategy(SingleTickerLongFlat):
    id = "market_structure_break"
    hypothesis = (
        "A close above the last major swing high breaks the market "
        "structure and starts a new up leg, as stops of short sellers get "
        "hit. Fails in ranges where swing highs keep holding."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 24

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="atr_lookback",
                kind="int",
                default=24,
                bounds=(14, 500),
                description="ATR window in bars; one ATR of counter-move confirms a "
                "base-level swing point.",
            ),
            ParameterSpec(
                name="level",
                kind="int",
                default=1,
                bounds=(0, LEVELS - 1),
                description="Structure level traded (0 = base swings; higher = larger swings).",
            ),
            *common_specs("BTC-USD.CC"),
        ]

    def history_bars(self) -> int:
        """Bars replayed per evaluation."""
        return max(3000, 20 * int(self.params["atr_lookback"]))

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        atr_lookback = int(self.params["atr_lookback"])
        bars = cache.last_n_bars(ticker, self.interval, as_of, self.history_bars())
        if len(bars) <= atr_lookback:
            return None
        high, low, close = (bars[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
        hi, lo = hierarchical_level_prices(
            high, low, close, atr_lookback, int(self.params["level"]), LEVELS
        )
        pos = _positions_from_levels(close, hi, lo)
        level_high = float(hi[-1])
        long = pos[-1] > 0
        # how far the close has broken above the level high
        score = close[-1] / level_high - 1.0 if long and level_high > 0 else 0.0
        return {
            "close": float(close[-1]),
            "level_high": level_high,
            "level_low": float(lo[-1]),
            "position": float(pos[-1]),
            "signal": 1.0 if long else 0.0,
            "score": float(score),
        }
