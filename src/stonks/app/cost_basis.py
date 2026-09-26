"""Average cost per open position, replayed from the fill history.

Weighted-average method: a fill that opens or adds to a position folds its
price (plus its fee) into the average; a fill that reduces it realizes P&L
and leaves the average unchanged; a fill that crosses zero starts a new
basis at its own price. Closed positions have no cost.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

_EPS = 1e-9


@dataclass(frozen=True)
class FillLot:
    ticker: str
    #: Signed: positive for buys, negative for sells.
    quantity: float
    price: float
    fee: float = 0.0


def average_costs(fills: Iterable[FillLot]) -> dict[str, float]:
    """``{ticker: average cost per share}`` for every still-open position,
    replaying ``fills`` in the order given (oldest first)."""
    qty: dict[str, float] = {}
    basis: dict[str, float] = {}  # signed total cost of the open quantity
    for f in fills:
        held = qty.get(f.ticker, 0.0)
        new = held + f.quantity
        if abs(held) < _EPS or held * f.quantity > 0:
            # Opening or adding: fees make longs dearer and shorts cheaper.
            basis[f.ticker] = basis.get(f.ticker, 0.0) + f.quantity * f.price + f.fee
        elif abs(new) < _EPS:
            basis[f.ticker] = 0.0
        elif held * new < 0:
            # Crossed zero: the remainder is a fresh position at this price.
            basis[f.ticker] = new * f.price
        else:
            basis[f.ticker] = basis[f.ticker] * (new / held)
        qty[f.ticker] = new
    return {t: basis[t] / q for t, q in qty.items() if abs(q) >= _EPS}
