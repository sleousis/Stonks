"""Vendor-neutral rows for regulatory filings (roadmap 23.13).

Any source of company filings fills these: SEC EDGAR today. Times are naive
UTC. ``known_at`` is when the regulator accepted the filing, the earliest
moment anyone outside the company could read it (P12).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from stonks.ingest.schemas import FrozenRow

__all__ = [
    "CURRENT_REPORT_ITEMS",
    "AmountType",
    "CorporateFilingRow",
    "InstitutionalHoldingRow",
    "InvestmentDiscretion",
]

AmountType = Literal["shares", "principal"]
InvestmentDiscretion = Literal["sole", "defined", "other"]

#: Current report (8-K) item codes and what they announce. The codes are
#: set by the SEC's form, so they are the domain vocabulary, not a vendor's.
CURRENT_REPORT_ITEMS: dict[str, str] = {
    "1.01": "entry into a material agreement",
    "1.02": "termination of a material agreement",
    "1.03": "bankruptcy or receivership",
    "1.05": "material cybersecurity incident",
    "2.01": "completed acquisition or disposal of assets",
    "2.02": "results of operations (earnings)",
    "2.03": "new direct financial obligation",
    "2.04": "triggering event that accelerates an obligation",
    "2.05": "exit or disposal costs",
    "2.06": "material impairments",
    "3.01": "delisting or listing rule failure",
    "3.02": "unregistered sale of equity",
    "3.03": "material change to security holders' rights",
    "4.01": "change of auditor",
    "4.02": "earlier financial statements no longer reliable",
    "5.01": "change in control",
    "5.02": "departure or appointment of directors or officers",
    "5.03": "amendment to articles or bylaws",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}


class CorporateFilingRow(FrozenRow):
    """One filing a company made (``corporate_filings``)."""

    accession_number: str
    ticker: str
    issuer_cik: str
    #: The form type as filed: ``8-K``, ``10-Q``, ``4``, ``13F-HR``, ...
    form: str
    filing_date: date
    known_at: datetime
    period_of_report: date | None = None
    #: Current report item codes (``2.02``), empty for other forms.
    items: tuple[str, ...] = ()
    url: str | None = None


class InstitutionalHoldingRow(FrozenRow):
    """One line of a manager's quarterly holdings report
    (``institutional_holdings``)."""

    accession_number: str
    line: int = Field(ge=0)
    filer_cik: str
    filer_name: str | None = None
    report_period: date
    filing_date: date
    known_at: datetime
    cusip: str
    issuer_name: str | None = None
    security_class: str | None = None
    #: Filled when the lake knows the CUSIP.
    ticker: str | None = None
    amount: float | None = None
    amount_type: AmountType | None = None
    value_usd: float | None = None
    put_call: Literal["put", "call"] | None = None
    investment_discretion: InvestmentDiscretion | None = None
