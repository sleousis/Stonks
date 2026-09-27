"""One vendor-neutral instrument model for every tradable thing.

:class:`InstrumentSpec` says what an instrument is: its canonical symbol
(a ticker such as ``AAPL.US`` or an option contract id such as
``AAPL.US:2026-01-16:C:150``), asset class, kind, currency, exchange, tick
and lot size, multiplier, and for derivatives the underlying, expiry,
strike and right. Brokers and vendors keep their own keys for it in
``broker_ids`` (``{"ibkr": "265598"}``), so a broker adapter can cache its
contract lookups without its types leaking here.

A stock is ``InstrumentSpec.spot("AAPL.US")``: multiplier 1, no expiry.
An option is ``InstrumentSpec.for_option(contract)``; the option-specific
view (intrinsic value, OCC symbol, split adjustment) stays on
:class:`~stonks.core.options.OptionContract`, and
:meth:`InstrumentSpec.option_contract` converts back.

:class:`InstrumentBook` resolves specs by symbol, with a stock spec for
any symbol it does not know, so valuation code can ask any id for its
multiplier.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

from stonks.core.options import (
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    is_option_id,
    parse_contract_id,
)
from stonks.core.types import AssetClass

#: What kind of contract the instrument is. ``spot`` covers shares, ETFs,
#: crypto pairs and cash bonds; derivatives name themselves.
InstrumentKind = Literal["spot", "option", "future"]


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    asset_class: AssetClass = "equity"
    kind: InstrumentKind = "spot"
    currency: str = "USD"
    exchange: str | None = None
    tick_size: float = 0.01
    lot_size: float = 1.0
    multiplier: float = 1.0
    underlying: str | None = None
    expiry: date | None = None
    strike: float | None = None
    right: OptionRight | None = None
    style: ExerciseStyle | None = None
    settlement: Settlement | None = None
    #: ``(broker or vendor, its id)`` pairs, e.g. ``(("ibkr", "265598"),)``.
    broker_ids: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("an instrument needs a symbol")
        for name in ("tick_size", "lot_size", "multiplier"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be positive, got {value}")
        derivative = (self.underlying, self.expiry)
        if self.kind == "spot" and any(
            v is not None for v in (*derivative, self.strike, self.right)
        ):
            raise ValueError("a spot instrument has no underlying, expiry, strike or right")
        if self.kind == "option" and (
            self.underlying is None
            or self.expiry is None
            or self.strike is None
            or self.right is None
        ):
            raise ValueError("an option needs an underlying, expiry, strike and right")
        if self.kind == "future" and (self.underlying is None or self.expiry is None):
            raise ValueError("a future needs an underlying and an expiry")

    # ---- constructors -------------------------------------------------------------

    @classmethod
    def spot(
        cls,
        symbol: str,
        asset_class: AssetClass = "equity",
        *,
        currency: str = "USD",
        exchange: str | None = None,
        tick_size: float = 0.01,
        lot_size: float = 1.0,
    ) -> InstrumentSpec:
        return cls(
            symbol=symbol,
            asset_class=asset_class,
            currency=currency,
            exchange=exchange,
            tick_size=tick_size,
            lot_size=lot_size,
        )

    @classmethod
    def for_option(
        cls,
        contract: OptionContract,
        *,
        asset_class: AssetClass = "equity",
        exchange: str | None = None,
        tick_size: float = 0.01,
        broker_ids: Iterable[tuple[str, str]] = (),
    ) -> InstrumentSpec:
        return cls(
            symbol=contract.contract_id,
            asset_class=asset_class,
            kind="option",
            currency=contract.currency,
            exchange=exchange,
            tick_size=tick_size,
            multiplier=contract.multiplier,
            underlying=contract.underlying,
            expiry=contract.expiry,
            strike=contract.strike,
            right=contract.right,
            style=contract.style,
            settlement=contract.settlement,
            broker_ids=tuple(broker_ids),
        )

    # ---- views ------------------------------------------------------------------

    @property
    def is_option(self) -> bool:
        return self.kind == "option"

    def option_contract(self) -> OptionContract:
        """The option view of an option spec; raises for anything else."""
        if (
            self.kind != "option"
            or self.underlying is None
            or self.expiry is None
            or self.strike is None
            or self.right is None
        ):
            raise ValueError(f"{self.symbol} is not an option")
        return OptionContract(
            underlying=self.underlying,
            expiry=self.expiry,
            strike=self.strike,
            right=self.right,
            multiplier=self.multiplier,
            style=self.style or "american",
            settlement=self.settlement or "physical",
            currency=self.currency,
        )

    def broker_id(self, broker: str) -> str | None:
        return dict(self.broker_ids).get(broker)

    def with_broker_id(self, broker: str, broker_id: str) -> InstrumentSpec:
        ids = dict(self.broker_ids)
        ids[broker] = broker_id
        return replace(self, broker_ids=tuple(sorted(ids.items())))

    def notional(self, quantity: float, price: float) -> float:
        """Money value of ``quantity`` at a per-unit ``price``."""
        return quantity * price * self.multiplier


class InstrumentBook:
    """Specs by symbol. An unknown option id is parsed into an option spec,
    any other unknown symbol is a stock with multiplier 1."""

    def __init__(self, specs: Iterable[InstrumentSpec] = ()) -> None:
        self._specs: dict[str, InstrumentSpec] = {s.symbol: s for s in specs}

    def add(self, spec: InstrumentSpec) -> None:
        self._specs[spec.symbol] = spec

    def get(self, symbol: str) -> InstrumentSpec:
        known = self._specs.get(symbol)
        if known is not None:
            return known
        if is_option_id(symbol):
            return InstrumentSpec.for_option(parse_contract_id(symbol))
        return InstrumentSpec.spot(symbol)

    def multiplier(self, symbol: str) -> float:
        return self.get(symbol).multiplier

    def value(self, positions: Mapping[str, float], prices: Mapping[str, float]) -> float:
        """Positions at per-unit ``prices`` times each multiplier (unpriced
        positions count 0)."""
        return sum(self.get(s).notional(q, prices[s]) for s, q in positions.items() if s in prices)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._specs

    def __len__(self) -> int:
        return len(self._specs)
