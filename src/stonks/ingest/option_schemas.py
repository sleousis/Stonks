"""Canonical row for option chains (roadmap 17.1).

Every options :class:`~stonks.ingest.sources.base.DataSource` returns
:class:`OptionQuoteRow` values: one contract's end-of-day quote on one day,
with the contract's identity and the vendor's Greeks and implied vol.
Vendor symbols (OCC or broker variants) are mapped to the canonical
contract id at parse time; vendor fields with no analogue are dropped.

Vendors often send ``0`` for a Greek or an implied vol they could not
compute. Adapters pass ``None`` instead: a zero vol is never a real quote.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from stonks.core.options import ExerciseStyle, OptionContract, OptionRight, Settlement
from stonks.ingest.schemas import FrozenRow


class OptionQuoteRow(FrozenRow):
    underlying: str = Field(min_length=1)
    expiry: date
    strike: float = Field(gt=0)
    right: OptionRight
    multiplier: float = Field(default=100.0, gt=0)
    style: ExerciseStyle = "american"
    settlement: Settlement = "physical"
    currency: str | None = None
    exchange: str | None = None
    occ_symbol: str | None = None

    as_of: date
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    last: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    open_interest: float | None = Field(default=None, ge=0)
    underlying_price: float | None = Field(default=None, gt=0)
    iv: float | None = Field(default=None, gt=0)
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None

    @property
    def contract(self) -> OptionContract:
        return OptionContract(
            underlying=self.underlying,
            expiry=self.expiry,
            strike=self.strike,
            right=self.right,
            multiplier=self.multiplier,
            style=self.style,
            settlement=self.settlement,
            currency=self.currency or "USD",
        )

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id
