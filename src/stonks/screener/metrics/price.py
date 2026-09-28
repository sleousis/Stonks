"""Price and liquidity metrics from daily bars up to the screen date. Each
reads a column of :attr:`ScreenData.price_stats`, so a screen over thousands
of tickers is one query, not a loop per ticker."""

from __future__ import annotations

from typing import ClassVar

from stonks.screener.data import HALF, MONTH, QUARTER, YEAR, ScreenData
from stonks.screener.metrics.base import ScreenMetric


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
        return data.dollar_volume


class _TrailingReturn(ScreenMetric):
    sessions: ClassVar[int]

    def compute(self, data: ScreenData) -> dict[str, float]:
        return data.trailing_return(self.sessions)


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
        return data.volatility


class FromHigh52w(ScreenMetric):
    id = "from_high_52w"
    label = "From 52-week high"
    group = "price"
    unit = "percent"
    description = "Last adjusted close against the highest of the last 252 sessions (0 at a high)."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return data.from_high
