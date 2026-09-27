"""Borrow availability and fees for short sales (roadmap 16.1).

A short sale needs a locate: the broker must find shares to borrow, and the
lender charges a fee while the short is open. The ``BorrowSource`` seam
answers both for one ticker on one day:

- ``easy``: general collateral, borrowable at the normal rate;
- ``hard``: hard to borrow, available at a higher rate;
- ``none``: no locate, so no short sale.

``FlatBorrow`` is the static default for backtests and the simulated
broker: a fee per asset class, plus per-ticker ``hard`` and ``none`` lists
from settings (``BorrowSettings``) or explicit quotes. A lake-backed source
(``borrow_rates``) and a broker-reported one plug in behind the same ABC.

Fees accrue daily on the short's market value: ``|qty| x price x
fee_rate_annual / 360`` per calendar day (the US money-market convention).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import AssetClass

BorrowStatus = Literal["easy", "hard", "none"]

#: Days in the fee year (US money-market convention).
DAY_COUNT = 360.0


@dataclass(frozen=True)
class BorrowQuote:
    status: BorrowStatus
    #: Annual fee as a fraction of the short's market value (0.005 = 0.5 %).
    fee_rate_annual: float = 0.0
    #: Shares the lender can supply; ``None`` means no stated limit.
    available_shares: float | None = None

    @property
    def shortable(self) -> bool:
        return self.status != "none"


def daily_fee(quantity: float, price: float, quote: BorrowQuote, days: float = 1.0) -> float:
    """Borrow fee for ``days`` calendar days on ``|quantity|`` shares."""
    return abs(quantity) * price * quote.fee_rate_annual * days / DAY_COUNT


class BorrowSource(ABC):
    """Borrow status and fee for a ticker on a day. ``None`` means no data,
    which callers treat as ``none`` (no quote, no short)."""

    #: Whether quotes change over time (a recall can then force a cover).
    has_history: ClassVar[bool] = False

    @abstractmethod
    def quote(
        self, ticker: str, day: date, asset_class: AssetClass = "equity"
    ) -> BorrowQuote | None: ...


class BorrowSettings(BaseModel):
    """``FlatBorrow`` knobs: the general-collateral fee per asset class and
    the tickers that are hard to borrow or not borrowable at all."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fee_rate_annual: dict[AssetClass, float] = Field(
        default_factory=lambda: {"equity": 0.005, "crypto": 0.05, "commodity": 0.01, "bond": 0.005}
    )
    hard_fee_rate_annual: float = Field(default=0.05, ge=0.0)
    hard: tuple[str, ...] = ()
    none: tuple[str, ...] = ()

    def build(self) -> FlatBorrow:
        return FlatBorrow(self)


class FlatBorrow(BorrowSource):
    """Static quotes: ``overrides`` first, then the ``none`` and ``hard``
    lists, then the asset class's general-collateral fee (0 when the class
    has none configured)."""

    def __init__(
        self,
        settings: BorrowSettings | None = None,
        overrides: Mapping[str, BorrowQuote] | None = None,
    ) -> None:
        self.settings = settings or BorrowSettings()
        self._overrides = dict(overrides or {})

    def quote(
        self, ticker: str, day: date, asset_class: AssetClass = "equity"
    ) -> BorrowQuote | None:
        override = self._overrides.get(ticker)
        if override is not None:
            return override
        if ticker in self.settings.none:
            return BorrowQuote("none")
        if ticker in self.settings.hard:
            return BorrowQuote("hard", self.settings.hard_fee_rate_annual)
        return BorrowQuote("easy", self.settings.fee_rate_annual.get(asset_class, 0.0))
