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

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.extremes import hierarchical_level_prices
from stonks.strategies._common import LakeBarCaches, iso
from stonks.strategies.base import BaseStrategy

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


class MarketStructureBreakStrategy(BaseStrategy):
    id = "market_structure_break"
    applicable_asset_classes = ("crypto", "equity")

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()

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
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"],
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="AAPL.US",
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

    # ---- Strategy Protocol -------------------------------------------------

    def extract_features(self, ticker: str, as_of, lake: Any) -> Features:
        state = self._state(ticker, as_of, lake)
        if state is None:
            return Features(values={})
        close, level_high, level_low, position = state
        return Features(
            values={
                "close": close,
                "level_high": level_high,
                "level_low": level_low,
                "position": position,
            }
        )

    def estimate_return(self, ticker: str, as_of, lake: Any) -> float | None:
        if ticker != self.params["ticker"]:
            return None
        state = self._state(ticker, as_of, lake)
        if state is None:
            return None
        close, level_high, _level_low, position = state
        if position <= 0:
            return None
        if not np.isfinite(level_high) or level_high <= 0:
            return 1e-6
        return max(close / level_high - 1.0, 1e-6)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        target = self.params["ticker"]
        price = prices.get(target)
        holding = portfolio.positions.get(target, 0.0)
        if my_picks and price and price > 0 and holding <= 0 and portfolio.cash > 0:
            qty = (portfolio.cash * float(self.params["allocation"])) / price
            if qty <= 0:
                return []
            return [
                Order(
                    client_id=f"{self.id}:buy:{target}:{iso(as_of)}",
                    ticker=target,
                    side="buy",
                    quantity=qty,
                    order_type="market",
                    strategy_id=self.id,
                )
            ]
        if not my_picks and holding > 0:
            return [
                Order(
                    client_id=f"{self.id}:sell:{target}:{iso(as_of)}",
                    ticker=target,
                    side="sell",
                    quantity=holding,
                    order_type="market",
                    strategy_id=self.id,
                )
            ]
        return []

    # ---- internals ---------------------------------------------------------

    def history_bars(self) -> int:
        """Bars replayed per evaluation."""
        return max(3000, 20 * int(self.params["atr_lookback"]))

    def _state(self, ticker: str, as_of, lake: Any) -> tuple[float, float, float, float] | None:
        if lake is None:
            return None
        atr_lookback = int(self.params["atr_lookback"])
        interval = Interval.parse(self.params["interval"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, interval, as_of, self.history_bars()
        )
        if len(bars) <= atr_lookback:
            return None
        high, low, close = (bars[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
        hi, lo = hierarchical_level_prices(
            high, low, close, atr_lookback, int(self.params["level"]), LEVELS
        )
        pos = _positions_from_levels(close, hi, lo)
        return float(close[-1]), float(hi[-1]), float(lo[-1]), float(pos[-1])
