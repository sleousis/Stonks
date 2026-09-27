"""How a backtest shorts (roadmap 16.4): the margin model, the borrow
fees, and a multiplier on those fees for stress tests.

``ShortingSettings`` is off unless a caller sets it (``LabDataset.shorting``
in the lab). ``enable(broker)`` turns short selling on for a
``SimulatedBroker``; the backtest config must then say
``allow_short=True`` too.

``ScaledBorrow`` multiplies every fee of another ``BorrowSource``. The cost
stress uses it to charge 2x or 3x the borrow fees, and the go-live gate
reads the fee a lab run charged to check it was realistic.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.core.types import AssetClass, Portfolio
from stonks.execution.borrow import BorrowQuote, BorrowSettings, BorrowSource
from stonks.execution.margin import MarginSettings

if TYPE_CHECKING:
    from stonks.backtest.simulated_broker import SimulatedBroker


class ScaledBorrow(BorrowSource):
    """``inner``'s quotes with every fee multiplied by ``multiplier``."""

    has_history: ClassVar[bool] = False

    def __init__(self, inner: BorrowSource, multiplier: float) -> None:
        if multiplier < 0:
            raise ValueError(f"borrow multiplier must be >= 0, got {multiplier}")
        self.inner = inner
        self.multiplier = float(multiplier)
        self.has_history = inner.has_history  # type: ignore[misc]

    def quote(
        self, ticker: str, day: date, asset_class: AssetClass = "equity"
    ) -> BorrowQuote | None:
        quote = self.inner.quote(ticker, day, asset_class)
        if quote is None:
            return None
        return BorrowQuote(
            quote.status, quote.fee_rate_annual * self.multiplier, quote.available_shares
        )


class ShortingSettings(BaseModel):
    """A backtest that may short: the margin model (``reg_t`` by default),
    the borrow fees, and ``borrow_multiplier`` on every fee."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    margin: MarginSettings = MarginSettings(model="reg_t")
    borrow: BorrowSettings = BorrowSettings()
    borrow_multiplier: float = Field(default=1.0, ge=0.0)

    @model_validator(mode="after")
    def _margin_allows_shorts(self) -> ShortingSettings:
        if not self.margin.build().allows_short:
            raise ValueError(
                f"shorting needs a margin model that allows shorts, got {self.margin.model}"
            )
        return self

    def borrow_source(self) -> BorrowSource:
        source = self.borrow.build()
        return (
            source
            if self.borrow_multiplier == 1.0
            else ScaledBorrow(source, self.borrow_multiplier)
        )

    def equity_fee_rate(self) -> float:
        """The general-collateral equity fee these settings charge a year."""
        return float(self.borrow.fee_rate_annual.get("equity", 0.0)) * self.borrow_multiplier

    def scaled(self, multiplier: float) -> ShortingSettings:
        """A copy with the borrow fees multiplied by ``multiplier`` more."""
        return self.model_copy(update={"borrow_multiplier": self.borrow_multiplier * multiplier})

    def enable(self, broker: SimulatedBroker) -> SimulatedBroker:
        broker.enable_shorts(self.margin.build(), self.borrow_source())
        return broker

    def broker(self, portfolio: Portfolio) -> SimulatedBroker:
        """A plain simulated broker that may short under these settings."""
        from stonks.backtest.simulated_broker import SimulatedBroker

        return self.enable(SimulatedBroker(portfolio))
