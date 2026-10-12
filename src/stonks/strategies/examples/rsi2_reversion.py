"""Rsi2Reversion: Connors' short-term mean reversion (Connors and Alvarez,
*Short Term Trading Strategies That Work*, 2008).

Rules, per ticker, on adjusted daily closes:

1. **Entry.** When flat, buy when the close is above its
   ``trend_bars``-day simple average (200) and the ``rsi_bars``-day Wilder
   RSI (2) is below ``entry_rsi`` (10): a sharp dip inside an uptrend.
2. **Exit.** Sell at the first close above its ``exit_ma_bars``-day simple
   average (5), or when the RSI rises above ``exit_rsi`` (70).
3. **Stateless.** The position is replayed from the bars on every call. An
   exit bar always leaves the book flat (an entry on a bar that also meets
   an exit does not count), so the replay starts from the last exit bar
   and the answer does not depend on how far back the bars were read.

A held name scores ``1 - RSI_at_entry / 100``, so the deepest dips rank
first. Decide: equal weight across the current picks, bought with the free
cash, and sell names that stop being picks.

Made for broad equity index ETFs (SPY, QQQ, IWM, DIA), where Connors found
the edge. Single stocks work too, with more gap risk. Long only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass, Features, Order, Portfolio
from stonks.features.library import rsi
from stonks.strategies._common import LakeBarCaches, equal_weight_new_picks
from stonks.strategies.base import BaseStrategy

#: Extra bars read past the trend average before the window is widened.
_FIRST_EXTRA = 60


class Rsi2Reversion(BaseStrategy):
    id = "rsi2_reversion"
    title = "Short dip buying"
    summary = (
        "Buys a sharp two-day dip in a fund that is above its 200-day average and sells "
        "on the first bounce."
    )
    hypothesis = (
        "Short, sharp sell-offs inside a long uptrend overshoot and snap back "
        "within days: forced and nervous sellers push prices below value and "
        "liquidity providers are paid to take the other side (Connors and "
        "Alvarez). The 200-day filter keeps us out of falling markets. Fails "
        "in a crash that starts inside an uptrend, and costs eat the small "
        "average gain if trading is expensive."
    )
    alpha_family = "reversion"
    premise = "mean_reversion"
    label_horizon_bars = 5
    required_history_bars = 201
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity",)
    parallel_scoring = True

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()

    def param_metadata(self) -> dict[str, int]:
        return {"required_history_bars": int(self.params["trend_bars"]) + 1}

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="trend_bars",
                kind="int",
                default=200,
                bounds=(100, 250),
                tunable=False,
                description="Days in the long average the close must be above to buy.",
            ),
            ParameterSpec(
                name="rsi_bars",
                kind="int",
                default=2,
                bounds=(2, 5),
                tunable=False,
                description="Days in the RSI.",
            ),
            ParameterSpec(
                name="entry_rsi",
                kind="float",
                default=10.0,
                bounds=(2.0, 30.0),
                description="Buy when the RSI is below this level.",
            ),
            ParameterSpec(
                name="exit_rsi",
                kind="float",
                default=70.0,
                bounds=(30.0, 90.0),
                description="Sell when the RSI is above this level.",
            ),
            ParameterSpec(
                name="exit_ma_bars",
                kind="int",
                default=5,
                bounds=(3, 10),
                description="Sell at the first close above the average of this many days.",
            ),
        ]

    # ---- rules -----------------------------------------------------------------

    def _indicators(self, closes: np.ndarray) -> pd.DataFrame:
        s = pd.Series(closes, dtype=float)
        p = self.params
        trend = s.rolling(int(p["trend_bars"])).mean()
        short = s.rolling(int(p["exit_ma_bars"])).mean()
        r = rsi(s, int(p["rsi_bars"]))
        entry = (s > trend) & (r < float(p["entry_rsi"]))
        leave = (s > short) | (r > float(p["exit_rsi"]))
        return pd.DataFrame(
            {"close": s, "trend": trend, "short": short, "rsi": r, "entry": entry, "exit": leave}
        )

    @staticmethod
    def _replay(ind: pd.DataFrame) -> tuple[bool, float]:
        """``(long, RSI at entry)`` after the last bar."""
        entries = ind["entry"].to_numpy(dtype=bool)
        exits = ind["exit"].to_numpy(dtype=bool)
        rsis = ind["rsi"].to_numpy(dtype=float)
        long, at_entry = False, float("nan")
        for i in range(len(ind)):
            if exits[i]:
                long = False
            elif not long and entries[i]:
                long, at_entry = True, float(rsis[i])
        return long, at_entry

    def _state(self, ticker: str, as_of: Any, lake: Any) -> tuple[pd.DataFrame, bool, float] | None:
        if lake is None:
            return None
        cache = self._bar_caches.for_lake(lake)
        trend = int(self.params["trend_bars"])
        n = trend + _FIRST_EXTRA
        while True:
            closes = cache.last_n_closes(ticker, Interval.DAY_1, as_of, n)
            if len(closes) < trend or not np.isfinite(closes[-trend:]).all():
                return None
            ind = self._indicators(closes)
            # an exit after the trend average is defined pins the state
            settled = ind["exit"].to_numpy(dtype=bool)[trend - 1 :].any()
            if settled or len(closes) < n:
                break
            n *= 2
        long, at_entry = self._replay(ind)
        return ind, long, at_entry

    # ---- Strategy Protocol -------------------------------------------------------

    def extract_features(self, ticker: str, as_of: date, lake: Any) -> Features:
        state = self._state(ticker, as_of, lake)
        if state is None:
            return Features(values={})
        ind, long, _ = state
        last = ind.iloc[-1]
        values = {
            "close": float(last["close"]),
            "trend_ma": float(last["trend"]),
            "exit_ma": float(last["short"]),
            "rsi": float(last["rsi"]),
            "signal": 1.0 if long else 0.0,
        }
        return Features(values={k: v for k, v in values.items() if np.isfinite(v)})

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        state = self._state(ticker, as_of, lake)
        if state is None:
            return None
        _, long, at_entry = state
        if not long or not np.isfinite(at_entry):
            return None
        # deeper dips rank first, always positive while held
        return max(1.0 - at_entry / 100.0, 1e-6)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        return equal_weight_new_picks(self.id, my_picks, portfolio, prices, as_of)
