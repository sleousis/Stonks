"""DualMomentum: Antonacci's Global Equities Momentum (GEM).

Four ETFs, one holding at a time (Gary Antonacci, *Dual Momentum
Investing*, 2014):

1. **Signal day.** The latest month-end session the decision can see
   (RS-03: a daily decision sees its own bar, an intraday one only closed
   days). The pick is taken there and held until the next month end, so a
   mid-month decision repeats the month-end answer.
2. **Relative momentum.** The total return over ``lookback_months`` (21
   sessions a month) of a US equity ETF (``us_equity``, SPY) and an ex-US
   equity ETF (``ex_us_equity``, EFA), from adjusted closes. The better one
   is the equity candidate (ties go to the US ETF).
3. **Absolute momentum.** The candidate is held when its return beats the
   cash proxy's (``cash``, BIL) over the same window. Otherwise the book
   holds the bond ETF (``bond``, AGG).
4. **Rebalance** on the last session of each month: everything goes into
   the pick, anything else is sold.

Point in time (P12): every read goes through the look-ahead-safe bar cache,
sliced to the decision, and the signal day is a bar the decision has seen.
Missing data for a compared ETF (or for the bond ETF when it is the pick)
holds nothing rather than guessing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.features.momentum import is_quarter_rebalance_day
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.strategies._common import LakeBarCaches
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import (
    CrossSectionMemo,
    orders_from_constructor,
    session_cutoff,
)
from stonks.strategies.examples._forecast_trend import period_end_mask

#: Sessions in a month for the lookback.
SESSIONS_PER_MONTH = 21
#: Extra sessions read so the latest month end is always in the window.
_MONTH_SLACK = 30
#: A compared ETF's last bar may sit this many calendar days before the
#: signal day (a missing print) and still count.
_MAX_GAP_DAYS = 7
_MONTHLY = list(range(1, 13))
_TICKER_PARAMS = ("us_equity", "ex_us_equity", "cash", "bond")


class DualMomentum(BaseStrategy):
    id = "dual_momentum"
    title = "Dual momentum"
    summary = (
        "Holds US or world stocks, whichever rose more over the past year, while they beat "
        "cash, and bonds otherwise."
    )
    hypothesis = (
        "Equity markets trend at the 12-month horizon: the stronger of US and ex-US "
        "stocks keeps leading (relative momentum), and stocks that fail to beat cash "
        "tend to keep falling (absolute momentum, Antonacci). Moving to bonds then "
        "cuts the deep bear-market drawdowns. Fails in sharp V-shaped reversals and "
        "choppy years, where the monthly switch is late both ways."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = SESSIONS_PER_MONTH
    required_history_bars = 12 * SESSIONS_PER_MONTH + 1 + _MONTH_SLACK
    applicable_asset_classes = ("equity", "bond")

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        tickers = [str(self.params[name]).strip() for name in _TICKER_PARAMS]
        if not all(tickers):
            raise ValueError("dual_momentum needs all four tickers")
        if len(set(tickers)) != len(tickers):
            raise ValueError("dual_momentum needs four different tickers")
        self._bar_caches = LakeBarCaches()
        self._memo = CrossSectionMemo()

    def param_metadata(self) -> dict[str, int]:
        return {"required_history_bars": self._lookback() + 1 + _MONTH_SLACK}

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback_months",
                kind="int",
                default=12,
                bounds=(3, 12),
                description="Months of total return compared (21 sessions a month).",
            ),
            ParameterSpec(
                name="us_equity",
                kind="categorical",
                default="SPY.US",
                bounds=None,
                tunable=False,
                description="US equity ETF.",
            ),
            ParameterSpec(
                name="ex_us_equity",
                kind="categorical",
                default="EFA.US",
                bounds=None,
                tunable=False,
                description="Equity ETF outside the US.",
            ),
            ParameterSpec(
                name="cash",
                kind="categorical",
                default="BIL.US",
                bounds=None,
                tunable=False,
                description="Cash proxy the best equity must beat (a T-bill ETF).",
            ),
            ParameterSpec(
                name="bond",
                kind="categorical",
                default="AGG.US",
                bounds=None,
                tunable=False,
                description="Bond ETF held when no equity beats cash.",
            ),
        ]

    def data_tickers(self) -> tuple[str, ...]:
        return tuple(str(self.params[name]) for name in _TICKER_PARAMS)

    # ---- signals -----------------------------------------------------------------

    def pick(self, as_of: Any, lake: Any) -> str | None:
        """The ticker held at a decision on ``as_of``, or ``None``."""
        _, cutoff = session_cutoff(as_of)
        return self._memo.get(lake, cutoff, lambda: self._compute_pick(as_of, lake))

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return None
        return 1.0 if ticker == self.pick(as_of, lake) else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        if not is_quarter_rebalance_day(day, _MONTHLY):
            return []
        signals = {t: 1.0 for r, t in my_picks if r > 0}
        inp = ConstructionInput(
            signals={self.id: signals}, portfolio=portfolio, prices=prices, as_of=day
        )
        constructor = get_constructor("equal_weight_top_n", n=max(1, len(signals)))
        return orders_from_constructor(constructor, inp, strategy_id=self.id)

    # ---- internals ---------------------------------------------------------------

    def _lookback(self) -> int:
        return int(self.params["lookback_months"]) * SESSIONS_PER_MONTH

    def _compute_pick(self, as_of: Any, lake: Any) -> str | None:
        cache = self._bar_caches.for_lake(lake)
        lookback = self._lookback()
        window = lookback + 1 + _MONTH_SLACK
        us, ex_us, cash, bond = (str(self.params[name]) for name in _TICKER_PARAMS)
        frames = {
            t: cache.last_n_bars(t, Interval.DAY_1, as_of, window) for t in (us, ex_us, cash, bond)
        }
        anchor = frames[us]
        if anchor.empty:
            return None
        days = pd.to_datetime(anchor["timestamp"])
        ends = np.flatnonzero(period_end_mask(days, "equity", "month"))
        if not ends.size:
            return None
        signal_day = days.iloc[int(ends[-1])]
        returns = {t: _window_return(frames[t], signal_day, lookback) for t in (us, ex_us, cash)}
        if any(r is None for r in returns.values()):
            return None
        best = ex_us if returns[ex_us] > returns[us] else us  # type: ignore[operator]
        if returns[best] > returns[cash]:  # type: ignore[operator]
            return best
        return bond if _row_at(frames[bond], signal_day) is not None else None


def _row_at(frame: pd.DataFrame, signal_day: pd.Timestamp) -> int | None:
    """Position of ``frame``'s last bar on or before ``signal_day``, when it
    is no more than a few days older; else ``None``."""
    if frame.empty:
        return None
    stamps = pd.to_datetime(frame["timestamp"]).to_numpy(dtype="datetime64[us]")
    i = int(np.searchsorted(stamps, np.datetime64(signal_day, "us"), side="right")) - 1
    if i < 0 or signal_day - pd.Timestamp(stamps[i]) > pd.Timedelta(days=_MAX_GAP_DAYS):
        return None
    return i


def _window_return(frame: pd.DataFrame, signal_day: pd.Timestamp, lookback: int) -> float | None:
    """Total return over ``lookback`` bars ending at ``signal_day``."""
    i = _row_at(frame, signal_day)
    if i is None or i < lookback:
        return None
    closes = frame["close"].to_numpy(dtype=float)
    past, now = closes[i - lookback], closes[i]
    if not past > 0:
        return None
    return float(now / past - 1.0)
