"""Margin models: how much a book may borrow, and when it must cut back
(roadmap 16.1, ``docs/design/shorting.md`` section 5).

A ``MarginModel`` answers three questions about a signed portfolio:

- ``initial_requirement``: the equity a new position ties up;
- ``maintenance_requirement``: the equity the whole book must keep;
- ``excess_equity``: equity above the initial requirement of what is held,
  the room left for new positions.

Models live in a small registry (``register_margin_model``,
``get_margin_model``) and are built from ``MarginSettings``:

- ``cash`` (the default): no shorts and no leverage. The room is the cash
  itself and nothing is ever called. Today's long-only behaviour.
- ``reg_t``: US Regulation T. 50 % initial on longs and shorts, maintenance
  25 % long and 30 % short, with per-asset-class overrides (crypto shorts
  often need 100 % or more). Negative cash (a margin loan) pays
  ``debit_rate_annual``.

A book whose equity falls below its maintenance requirement is in breach by
``deficit``. ``cover_quantity`` says how much of one position to close to
cure a deficit.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import AssetClass, Portfolio

MarginModelName = Literal["cash", "reg_t"]


class MarginRates(BaseModel):
    """Requirement rates, as fractions of a position's market value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_long: float = Field(default=0.5, gt=0.0)
    initial_short: float = Field(default=0.5, gt=0.0)
    maintenance_long: float = Field(default=0.25, ge=0.0)
    maintenance_short: float = Field(default=0.30, ge=0.0)


class MarginSettings(BaseModel):
    """Which margin model a book uses, and its rates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: MarginModelName = "cash"
    rates: MarginRates = MarginRates()
    #: Per-asset-class rates that replace ``rates`` for that class.
    overrides: dict[AssetClass, MarginRates] = Field(default_factory=dict[AssetClass, MarginRates])
    #: Annual interest on negative cash (a margin loan).
    debit_rate_annual: float = Field(default=0.0, ge=0.0)

    def build(self) -> MarginModel:
        return get_margin_model(self.model, self)


def _value(prices: Mapping[str, float], ticker: str) -> float:
    price = prices.get(ticker)
    try:
        value = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


class MarginModel(ABC):
    """See the module doc. ``quantity`` is signed (negative for a short)."""

    name: ClassVar[str]
    #: Whether the model lets a book hold short positions at all.
    allows_short: ClassVar[bool] = False

    def __init__(self, settings: MarginSettings | None = None) -> None:
        self.settings = settings or MarginSettings(model=self.name)  # type: ignore[arg-type]

    @property
    def debit_rate_annual(self) -> float:
        return self.settings.debit_rate_annual

    @abstractmethod
    def initial_requirement(
        self, ticker: str, quantity: float, price: float, asset_class: AssetClass = "equity"
    ) -> float: ...

    @abstractmethod
    def maintenance_requirement(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float: ...

    @abstractmethod
    def excess_equity(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float: ...

    def buying_power(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float:
        """Notional of a new equity long the excess equity could carry."""
        rate = self.initial_requirement("", 1.0, 1.0)
        return max(self.excess_equity(portfolio, prices, asset_classes), 0.0) / rate

    def deficit(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float:
        """How far equity sits below the maintenance requirement (0 when
        the book is in good standing)."""
        equity = portfolio.total_value(prices)
        return max(self.maintenance_requirement(portfolio, prices, asset_classes) - equity, 0.0)

    def maintenance_rate(self, quantity: float, asset_class: AssetClass = "equity") -> float:
        """The maintenance rate of a long (``quantity > 0``) or short."""
        return 0.0

    def cover_quantity(
        self, quantity: float, price: float, deficit: float, asset_class: AssetClass = "equity"
    ) -> float:
        """Shares of a position of ``quantity`` to close to cure ``deficit``,
        ignoring costs: closing frees its maintenance requirement and leaves
        equity unchanged. Capped at the whole position."""
        freed = self.maintenance_rate(quantity, asset_class) * price
        if deficit <= 0:
            return 0.0
        if freed <= 0:
            return abs(quantity)
        return min(deficit / freed, abs(quantity))


class CashMargin(MarginModel):
    """A cash account: every long is paid in full, shorts are refused, and
    the room for new positions is the cash."""

    name = "cash"
    allows_short = False

    def initial_requirement(
        self, ticker: str, quantity: float, price: float, asset_class: AssetClass = "equity"
    ) -> float:
        return abs(quantity) * price

    def maintenance_requirement(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float:
        return 0.0

    def excess_equity(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float:
        return portfolio.cash


class RegTMargin(MarginModel):
    """Regulation T style margin; see the module doc."""

    name = "reg_t"
    allows_short = True

    def rates(self, asset_class: str = "equity") -> MarginRates:
        return self.settings.overrides.get(asset_class, self.settings.rates)  # type: ignore[call-overload]

    def initial_requirement(
        self, ticker: str, quantity: float, price: float, asset_class: AssetClass = "equity"
    ) -> float:
        r = self.rates(asset_class)
        rate = r.initial_long if quantity >= 0 else r.initial_short
        return abs(quantity) * price * rate

    def maintenance_rate(self, quantity: float, asset_class: AssetClass = "equity") -> float:
        r = self.rates(asset_class)
        return r.maintenance_long if quantity >= 0 else r.maintenance_short

    def maintenance_requirement(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float:
        classes = asset_classes or {}
        return sum(
            abs(q) * _value(prices, t) * self.maintenance_rate(q, classes.get(t, "equity"))  # type: ignore[arg-type]
            for t, q in portfolio.positions.items()
        )

    def excess_equity(
        self,
        portfolio: Portfolio,
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str] | None = None,
    ) -> float:
        classes = asset_classes or {}
        used = sum(
            self.initial_requirement(t, q, _value(prices, t), classes.get(t, "equity"))  # type: ignore[arg-type]
            for t, q in portfolio.positions.items()
        )
        return portfolio.total_value(prices) - used


# ---- registry -----------------------------------------------------------------------

_MODELS: dict[str, type[MarginModel]] = {}


def register_margin_model(cls: type[MarginModel]) -> type[MarginModel]:
    """Class decorator: make ``cls`` buildable by its ``name``."""
    _MODELS[cls.name] = cls
    return cls


def get_margin_model(name: str, settings: MarginSettings | None = None) -> MarginModel:
    cls = _MODELS.get(name)
    if cls is None:
        raise ValueError(f"unknown margin model {name!r}; choose one of {sorted(_MODELS)}")
    return cls(settings)


register_margin_model(CashMargin)
register_margin_model(RegTMargin)
