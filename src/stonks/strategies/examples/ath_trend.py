"""AllTimeHighTrend — buy weekly closes at all-time highs, exit on a wide
ATR trailing stop (BL-40; Wilcox & Crittenden, *Does Trend Following Work
on Stocks?*; Covel).

Rules, per ticker, on adjusted daily bars (all of the ticker's history in
the lake up to ``as_of``):

1. **Entry.** When flat, buy at the close of a week's last session whose
   close is at an all-time high (at or above every earlier close). Only
   after ``min_history_bars`` (252) bars, so a fresh listing isn't at a
   "high" on its first days.
2. **Exit.** A trailing stop ``HWM_since_entry - k_atr * ATR(atr_bars)``
   (10 x the 50-day Wilder ATR), which never moves down, checked on every
   daily close: sell once a close falls below the stop in force (see
   :mod:`stonks.features.trailing_stop`). Re-enter only on a later weekly
   all-time-high close.
3. **Stateless.** The position is replayed from the bars on every call, so
   a restart or a fresh instance holds exactly what the rules say.
4. Held names carry a forecast of 10 (``estimate_return``); sizing is
   volatility-targeted through ``vol_target`` like the other BL-40
   strategies (see :mod:`stonks.strategies.examples._forecast_trend`).

Long only by construction (the book's system has no short side). Applies
to equities and crypto, the book's evidence base.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass
from stonks.features.indicators import atr
from stonks.features.trailing_stop import Trade, open_trade, replay_trades
from stonks.portfolio.signals import FORECAST_TARGET
from stonks.strategies.examples._cross_section import session_cutoff
from stonks.strategies.examples._forecast_trend import (
    ForecastTrendStrategy,
    period_end_mask,
    sizing_specs,
)


class AllTimeHighTrend(ForecastTrendStrategy):
    id = "ath_trend"
    hypothesis = (
        "Stocks and coins making new all-time highs keep trending: anchoring "
        "on the old high and disposition-effect selling slow the move, and a "
        "rare few huge winners pay for many small losses (Wilcox & "
        "Crittenden). The 10-ATR stop lets winners run. We are paid by "
        "sellers who take profits early. Fails in choppy markets that make "
        "marginal new highs and reverse, and costs bite on false breakouts."
    )
    label_horizon_bars = 63
    required_history_bars = 252
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity", "crypto")
    #: The book's system has no short side, so ``short_mode`` stays ``flat``.
    short_capable: ClassVar[bool] = False

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="atr_bars",
                kind="int",
                default=50,
                bounds=(40, 60),
                description="Wilder ATR period of the trailing stop.",
            ),
            ParameterSpec(
                name="k_atr",
                kind="float",
                default=10.0,
                bounds=(3.0, 12.0),
                description="Stop distance below the high-water mark, in ATRs.",
            ),
            ParameterSpec(
                name="min_history_bars",
                kind="int",
                default=252,
                bounds=(50, 2520),
                tunable=False,
                description="Bars of history before an all-time high counts.",
            ),
            *sizing_specs(),
        ]

    def _history_bars(self) -> None:
        return None  # all-time: every bar up to as_of

    def _min_bars(self) -> int:
        return int(self.params["min_history_bars"])

    def replay(self, bars: pd.DataFrame, asset_class: str) -> list[Trade]:
        """Every trade the rules take over ``bars`` (oldest first)."""
        closes = bars["close"].to_numpy(dtype=float)
        atrs = atr(
            bars["high"].astype(float),
            bars["low"].astype(float),
            bars["close"].astype(float),
            int(self.params["atr_bars"]),
        ).to_numpy()
        week_end = period_end_mask(pd.to_datetime(bars["timestamp"]), asset_class, "week")
        at_high = closes >= np.maximum.accumulate(closes)
        seasoned = np.arange(len(closes)) >= int(self.params["min_history_bars"]) - 1
        return replay_trades(
            closes, atrs, week_end & at_high & seasoned, float(self.params["k_atr"])
        )

    def trade_log(self, ticker: str, as_of: Any, lake: Any) -> list[Trade]:
        """The replayed trades of ``ticker`` from bars dated on or before
        ``as_of`` (bar indices into its full daily history)."""
        _, cutoff = session_cutoff(as_of)
        bars = self._bars(self._bar_caches.for_lake(lake), ticker, cutoff)
        if bars.empty:
            return []
        return self.replay(bars.reset_index(drop=True), self._asset_class(ticker, lake))

    def _signed_forecast(self, bars: pd.DataFrame, asset_class: str) -> float | None:
        return FORECAST_TARGET if open_trade(self.replay(bars, asset_class)) else 0.0
