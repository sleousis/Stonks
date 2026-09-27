"""The book insights read: cash plus holdings, each already valued.

A holding keeps the broker's own symbol when no ticker maps to it (a synced
account's "not covered" position), so insights can show it without pricing
or scoring it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["long", "short"]


@dataclass(frozen=True)
class Holding:
    symbol: str
    ticker: str | None
    quantity: float
    price: float | None
    market_value: float | None
    asset_class: str | None = None
    sector: str | None = None
    currency: str | None = None

    @property
    def side(self) -> Side:
        return "short" if self.quantity < 0 else "long"

    @property
    def priced(self) -> bool:
        return self.market_value is not None


@dataclass(frozen=True)
class Book:
    cash: float
    holdings: tuple[Holding, ...]
    base_currency: str = "USD"

    @property
    def priced(self) -> tuple[Holding, ...]:
        return tuple(h for h in self.holdings if h.priced)

    @property
    def unpriced(self) -> tuple[Holding, ...]:
        return tuple(h for h in self.holdings if not h.priced)

    @property
    def uncovered(self) -> tuple[Holding, ...]:
        """Holdings no ticker maps to."""
        return tuple(h for h in self.holdings if h.ticker is None)

    @property
    def invested(self) -> float:
        return sum(h.market_value or 0.0 for h in self.holdings)

    @property
    def gross_invested(self) -> float:
        return sum(abs(h.market_value or 0.0) for h in self.holdings)

    @property
    def total_value(self) -> float:
        return self.cash + self.invested
