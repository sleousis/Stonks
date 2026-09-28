"""Average cost per open position, replayed from the fill history.

Weighted-average method: a fill that opens or adds to a position folds its
price (plus its fee) into the average; a fill that reduces it realizes P&L
and leaves the average unchanged; a fill that crosses zero starts a new
basis at its own price. Closed positions have no cost. A split multiplies
the held quantity by its ratio and keeps the total cost, so the average
follows the shares the book holds after it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

_EPS = 1e-9


@dataclass(frozen=True)
class FillLot:
    ticker: str
    #: Signed: positive for buys, negative for sells.
    quantity: float
    price: float
    fee: float = 0.0
    #: The fill's day, so splits on or before it apply first.
    day: date | None = None


@dataclass(frozen=True)
class SplitEvent:
    ticker: str
    ex_date: date
    #: New shares per old share.
    ratio: float


def average_costs(fills: Iterable[FillLot], splits: Iterable[SplitEvent] = ()) -> dict[str, float]:
    """``{ticker: average cost per share}`` for every still-open position,
    replaying ``fills`` in the order given (oldest first). Each of
    ``splits`` (the ones applied to the book) scales the held quantity
    before the first fill on or after its ex-date, else at the end."""
    qty: dict[str, float] = {}
    basis: dict[str, float] = {}  # signed total cost of the open quantity
    due: dict[str, list[SplitEvent]] = {}
    for sp in sorted(splits, key=lambda x: x.ex_date):
        if sp.ratio > 0:
            due.setdefault(sp.ticker, []).append(sp)

    def split_until(ticker: str, day: date | None) -> None:
        waiting = due.get(ticker)
        while waiting and (day is None or waiting[0].ex_date <= day):
            qty[ticker] = qty.get(ticker, 0.0) * waiting.pop(0).ratio

    for f in fills:
        if f.day is not None:
            split_until(f.ticker, f.day)
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
    for ticker in list(due):
        split_until(ticker, None)
    return {t: basis[t] / q for t, q in qty.items() if abs(q) >= _EPS}
