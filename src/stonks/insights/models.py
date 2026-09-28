"""Result types of the insights package. They are pydantic models so the
API returns them as they are (one definition, no view copies)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

AllocationKey = Literal["asset_class", "sector", "currency", "ticker"]
Period = Literal["1d", "1w", "1m", "3m", "ytd", "1y", "inception"]
Stance = Literal["agree", "disagree", "no_view", "not_applicable", "error"]


class AllocationSlice(BaseModel):
    key: str = Field(description="The group: an asset class, sector, currency or ticker.")
    value: float = Field(description="Market value of the group (shorts count negative).")
    weight: float | None = Field(
        description="value / the book's total value; null when the total is zero or less."
    )
    holdings: int = Field(description="Holdings in the group (0 for cash).")


class Exposure(BaseModel):
    long_value: float
    short_value: float = Field(description="Market value of short holdings (zero or negative).")
    gross: float | None = Field(description="(long - short) / total value; null without value.")
    net: float | None = Field(description="(long + short) / total value; null without value.")
    beta: float | None = Field(
        description="Sum of each holding's weight times its beta to the benchmark, over the "
        "holdings with a beta. Null when none has one."
    )
    beta_coverage: float = Field(
        description="Share of the gross holdings (by value) that have a beta."
    )
    benchmark: str | None


class PeriodPnl(BaseModel):
    period: Period
    start_day: date | None = Field(description="Day of the start value; null without history.")
    end_day: date
    start_value: float | None
    end_value: float
    change: float | None = Field(description="Value change, deposits and withdrawals included.")
    change_pct: float | None = Field(description="change / start_value (0.05 = +5%).")
    net_flows: float = Field(
        default=0.0, description="Deposits less withdrawals inside the period (roadmap 20.5)."
    )
    twr: float | None = Field(
        default=None,
        description="Time-weighted return over the period: deposits and withdrawals taken out, "
        "so a deposit is never profit. Null without a start value.",
    )


class MonthlyReturn(BaseModel):
    month: str = Field(description="YYYY-MM")
    #: The month's return; null when it cannot be measured.
    value: float | None


class RiskStats(BaseModel):
    observations: int = Field(description="Daily returns the numbers use.")
    volatility: float | None = Field(description="Annualized (252 days) standard deviation.")
    max_drawdown: float = Field(description="Worst fall from a peak, <= 0.")
    current_drawdown: float = Field(description="Latest value against its peak, <= 0.")
    var_95: float = Field(description="One-day historical value at risk, a return (loss < 0).")
    expected_shortfall_95: float = Field(description="Mean return at or below var_95.")


class Concentration(BaseModel):
    holdings: int = Field(description="Priced holdings.")
    largest: str | None
    top_weight: float | None = Field(description="Largest holding's share of gross holdings.")
    top5_weight: float | None
    hhi: float | None = Field(description="Herfindahl index of the holding weights (0 to 1).")
    effective_holdings: float | None = Field(description="1 / hhi.")


class Opinion(BaseModel):
    strategy_id: str
    stance: Stance
    expected_return: float | None
    reason: str


class HoldingAgreement(BaseModel):
    symbol: str
    ticker: str | None
    side: Literal["long", "short"]
    agree: int
    disagree: int
    opinions: list[Opinion]
