"""Multi-leg option orders (roadmap 17.3).

A :class:`ComboOrder` is one unit of trading: all of its legs fill or none
do. Each leg is an option contract or the underlying's shares, a ratio and
a side; ``quantity`` is the number of combo units. A vertical spread is
``[(buy call 100, 1), (sell call 110, 1)]`` and a buy-write is ``[(buy 100
shares, 100), (sell call, 1)]``.

``net_limit`` is the most the combo may cost per unit, per share of the
option contracts (the way brokers quote combos): positive for a debit,
negative for a credit (``-1.20`` means "receive at least 1.20"). ``None``
fills at whatever the quotes give.

:meth:`ComboOrder.leg_orders` turns a combo into plain
:class:`~stonks.core.types.Order` values (the ticker is the contract id or
the share ticker, the quantity is contracts or shares) carrying
``combo_id`` and ``structure`` in their decision context, which is how the
option risk rules see a combo as one unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from stonks.core.options import OptionContract
from stonks.core.types import Order, OrderSide, PositionEffect

ComboEffect = Literal["open", "close"]


@dataclass(frozen=True)
class ComboLeg:
    side: OrderSide
    ratio: float
    contract: OptionContract | None = None
    #: The share ticker for a stock leg (``contract`` is then ``None``).
    shares: str | None = None

    def __post_init__(self) -> None:
        if (self.contract is None) == (self.shares is None):
            raise ValueError("a leg is either an option contract or shares, not both")
        if self.ratio <= 0:
            raise ValueError(f"leg ratio must be positive, got {self.ratio}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"leg side must be buy or sell, got {self.side!r}")

    @property
    def instrument(self) -> str:
        return self.contract.contract_id if self.contract is not None else str(self.shares)

    @property
    def is_option(self) -> bool:
        return self.contract is not None

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1

    def reversed(self) -> ComboLeg:
        return ComboLeg(
            side="sell" if self.side == "buy" else "buy",
            ratio=self.ratio,
            contract=self.contract,
            shares=self.shares,
        )


@dataclass(frozen=True)
class ComboOrder:
    client_id: str
    legs: tuple[ComboLeg, ...]
    quantity: float = 1.0
    structure: str = "custom"
    net_limit: float | None = None
    #: The position group the legs open or close.
    group_id: str | None = None
    effect: ComboEffect = "open"
    strategy_id: str | None = None
    decided_at: date | None = None
    reason: str = ""
    tags: dict[str, str] = field(default_factory=dict[str, str], compare=False)

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("a combo needs at least one leg")
        if self.quantity <= 0:
            raise ValueError(f"combo quantity must be positive, got {self.quantity}")
        if self.effect not in ("open", "close"):
            raise ValueError(f"combo effect must be open or close, got {self.effect!r}")
        ids = [leg.instrument for leg in self.legs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"a combo lists an instrument twice: {ids}")

    @property
    def option_legs(self) -> tuple[ComboLeg, ...]:
        return tuple(leg for leg in self.legs if leg.is_option)

    @property
    def underlyings(self) -> set[str]:
        out = {leg.contract.underlying for leg in self.legs if leg.contract is not None}
        out |= {str(leg.shares) for leg in self.legs if leg.shares is not None}
        return out

    def signed_quantities(self) -> dict[str, float]:
        """Instrument -> signed contracts or shares for the whole order."""
        return {leg.instrument: leg.sign * leg.ratio * self.quantity for leg in self.legs}

    def leg_orders(self) -> list[Order]:
        effect: PositionEffect = self.effect
        return [
            Order(
                client_id=f"{self.client_id}:{i}",
                ticker=leg.instrument,
                side=leg.side,
                quantity=leg.ratio * self.quantity,
                strategy_id=self.strategy_id,
                position_effect=effect,
                decision_context={
                    "combo_id": self.client_id,
                    "structure": self.structure,
                    "group_id": self.group_id or self.client_id,
                },
            )
            for i, leg in enumerate(self.legs)
        ]


def combo_id_of(order: Order) -> str:
    """The combo an order belongs to (its own client id when alone)."""
    ctx = order.decision_context or {}
    return str(ctx.get("combo_id") or order.client_id)
