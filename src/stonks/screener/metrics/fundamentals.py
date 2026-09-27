"""Valuation and quality metrics from statements known on the screen date
(trailing twelve months for flows, the latest balance sheet for stocks)."""

from __future__ import annotations

from stonks.screener.data import ScreenData
from stonks.screener.metrics.base import ScreenMetric


def _ratio(
    top: dict[str, float], bottom: dict[str, float], *, positive_bottom: bool = True
) -> dict[str, float]:
    return {
        t: top[t] / b
        for t, b in bottom.items()
        if t in top and (b > 0 if positive_bottom else b != 0)
    }


class MarketCap(ScreenMetric):
    id = "market_cap"
    label = "Market cap"
    group = "fundamental"
    unit = "money"
    description = "Stored market cap near the date, else price times shares outstanding."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return data.market_cap


class PeRatio(ScreenMetric):
    id = "pe_ratio"
    label = "P/E"
    group = "fundamental"
    unit = "ratio"
    description = "Market cap over trailing net income. No value for a loss."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return _ratio(data.market_cap, data.column(data.income, "net_income"))


class PbRatio(ScreenMetric):
    id = "pb_ratio"
    label = "P/B"
    group = "fundamental"
    unit = "ratio"
    description = "Market cap over shareholders' equity. No value for negative equity."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return _ratio(data.market_cap, data.column(data.balance, "total_stockholder_equity"))


class PsRatio(ScreenMetric):
    id = "ps_ratio"
    label = "P/S"
    group = "fundamental"
    unit = "ratio"
    description = "Market cap over trailing revenue."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return _ratio(data.market_cap, data.column(data.income, "revenue"))


class DividendYield(ScreenMetric):
    id = "dividend_yield"
    label = "Dividend yield"
    group = "fundamental"
    unit = "percent"
    description = "Dividends with an ex date in the last year over the last close."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return _ratio(data.dividends_ttm, data.last_close)


class NetMargin(ScreenMetric):
    id = "net_margin"
    label = "Net margin"
    group = "fundamental"
    unit = "percent"
    description = "Trailing net income over trailing revenue."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return _ratio(data.column(data.income, "net_income"), data.column(data.income, "revenue"))


class ReturnOnEquity(ScreenMetric):
    id = "roe"
    label = "Return on equity"
    group = "fundamental"
    unit = "percent"
    description = "Trailing net income over shareholders' equity."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return _ratio(
            data.column(data.income, "net_income"),
            data.column(data.balance, "total_stockholder_equity"),
        )


class DebtToEquity(ScreenMetric):
    id = "debt_to_equity"
    label = "Debt to equity"
    group = "fundamental"
    unit = "ratio"
    description = "Total debt over shareholders' equity."

    def compute(self, data: ScreenData) -> dict[str, float]:
        return _ratio(
            data.column(data.balance, "total_debt"),
            data.column(data.balance, "total_stockholder_equity"),
        )


class RevenueGrowth(ScreenMetric):
    id = "revenue_growth"
    label = "Revenue growth"
    group = "fundamental"
    unit = "percent"
    description = "Trailing revenue against the twelve months before."

    def compute(self, data: ScreenData) -> dict[str, float]:
        now = data.column(data.income, "revenue")
        return {
            t: v - 1.0 for t, v in _ratio(now, data.column(data.income, "revenue_prior")).items()
        }
