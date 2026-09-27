"""Price and liquidity metrics from daily bars up to the screen date."""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from stonks.screener.data import ScreenData
from stonks.screener.metrics.base import ScreenMetric

#: Sessions in a month, a quarter, half a year and a year.
MONTH, QUARTER, HALF, YEAR = 21, 63, 126, 252


class Price(ScreenMetric):
    id = "price"
    label = "Price"
    group = "price"
    unit = "money"
    description = "Last daily close on or before the date."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return data.last_close


class DollarVolume20d(ScreenMetric):
    id = "dollar_volume_20d"
    label = "Dollar volume (20 days)"
    group = "price"
    unit = "money"
    description = "Average close times volume over the last 20 sessions."

    def compute(self, data: ScreenData) -> dict[str, float]:
        df = data.bars.dropna(subset=["close", "volume"])
        if df.empty:
            return {}
        traded = (df["close"] * df["volume"]).groupby(df["ticker"])
        return traded.apply(lambda s: s.tail(20).mean()).to_dict()


class _TrailingReturn(ScreenMetric):
    sessions: ClassVar[int]

    def compute(self, data: ScreenData) -> dict[str, float]:
        n = self.sessions
        return {
            t: float(p[-1] / p[-1 - n] - 1.0)
            for t, p in data.adjusted.items()
            if len(p) > n and p[-1 - n] > 0
        }


class Return1m(_TrailingReturn):
    id = "return_1m"
    label = "Return 1 month"
    group = "price"
    unit = "percent"
    description = "Total return over the last 21 sessions (adjusted closes)."
    sessions = MONTH


class Return3m(_TrailingReturn):
    id = "return_3m"
    label = "Return 3 months"
    group = "price"
    unit = "percent"
    description = "Total return over the last 63 sessions (adjusted closes)."
    sessions = QUARTER


class Return6m(_TrailingReturn):
    id = "return_6m"
    label = "Return 6 months"
    group = "price"
    unit = "percent"
    description = "Total return over the last 126 sessions (adjusted closes)."
    sessions = HALF


class Return12m(_TrailingReturn):
    id = "return_12m"
    label = "Return 12 months"
    group = "price"
    unit = "percent"
    description = "Total return over the last 252 sessions (adjusted closes)."
    sessions = YEAR


class Volatility3m(ScreenMetric):
    id = "volatility_3m"
    label = "Volatility 3 months"
    group = "price"
    unit = "percent"
    description = "Annualised standard deviation of daily log returns over 63 sessions."

    def compute(self, data: ScreenData) -> dict[str, float]:
        out: dict[str, float] = {}
        for t, p in data.adjusted.items():
            window = p[-(QUARTER + 1) :]
            if len(window) < 21 or (window <= 0).any():
                continue
            out[t] = float(np.diff(np.log(window)).std(ddof=1) * np.sqrt(YEAR))
        return out


class FromHigh52w(ScreenMetric):
    id = "from_high_52w"
    label = "From 52-week high"
    group = "price"
    unit = "percent"
    description = "Last adjusted close against the highest of the last 252 sessions (0 at a high)."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return {
            t: float(p[-1] / p[-YEAR:].max() - 1.0)
            for t, p in data.adjusted.items()
            if len(p) and p[-YEAR:].max() > 0
        }
