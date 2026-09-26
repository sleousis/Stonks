"""Portfolio insights (roadmap 15.4): allocation, exposure, P&L over
periods, risk and strategy agreement for one book.

Pure analysis: every function takes a :class:`Book`, values or returns and
touches no store. ``stonks.app.insights`` loads a portfolio the caller owns
(a simulated book or a synced broker account) and calls these.
"""

from stonks.insights.agreement import judge, strategy_agreement
from stonks.insights.allocation import allocation, exposure
from stonks.insights.book import Book, Holding
from stonks.insights.models import (
    AllocationKey,
    AllocationSlice,
    Concentration,
    Exposure,
    HoldingAgreement,
    Opinion,
    PeriodPnl,
    RiskStats,
)
from stonks.insights.pnl import period_pnl
from stonks.insights.risk import (
    beta,
    concentration,
    realized_risk,
    returns_risk,
    weighted_returns,
)

__all__ = [
    "AllocationKey",
    "AllocationSlice",
    "Book",
    "Concentration",
    "Exposure",
    "Holding",
    "HoldingAgreement",
    "Opinion",
    "PeriodPnl",
    "RiskStats",
    "allocation",
    "beta",
    "concentration",
    "exposure",
    "judge",
    "period_pnl",
    "realized_risk",
    "returns_risk",
    "strategy_agreement",
    "weighted_returns",
]
