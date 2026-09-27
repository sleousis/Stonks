"""Allocation and exposure of a book."""

from __future__ import annotations

from collections.abc import Mapping

from stonks.insights.book import Book, Holding
from stonks.insights.models import AllocationKey, AllocationSlice, Exposure

CASH = "cash"
UNKNOWN = "unknown"


def _key(holding: Holding, by: AllocationKey) -> str:
    if by == "ticker":
        return holding.symbol
    value = getattr(holding, by)
    return str(value) if value else UNKNOWN


def allocation(book: Book, by: AllocationKey) -> list[AllocationSlice]:
    """Priced holdings grouped by ``by``, plus cash, largest absolute value
    first. Cash is its own group, except by currency, where it counts in
    the book's base currency."""
    values: dict[str, float] = {}
    counts: dict[str, int] = {}
    for h in book.priced:
        key = _key(h, by)
        values[key] = values.get(key, 0.0) + (h.market_value or 0.0)
        counts[key] = counts.get(key, 0) + 1
    if book.cash:
        key = book.base_currency if by == "currency" else CASH
        values[key] = values.get(key, 0.0) + book.cash
        counts.setdefault(key, 0)
    total = book.total_value
    slices = [
        AllocationSlice(
            key=key,
            value=value,
            weight=value / total if total > 0 else None,
            holdings=counts[key],
        )
        for key, value in values.items()
    ]
    return sorted(slices, key=lambda s: (-abs(s.value), s.key))


def exposure(book: Book, *, betas: Mapping[str, float | None], benchmark: str | None) -> Exposure:
    """Long, short, gross and net exposure as shares of the book's value, and
    the book's beta from each holding's beta (``betas`` by symbol)."""
    long_value = sum(h.market_value or 0.0 for h in book.priced if (h.market_value or 0.0) > 0)
    short_value = sum(h.market_value or 0.0 for h in book.priced if (h.market_value or 0.0) < 0)
    total = book.total_value
    weighted = 0.0
    covered = 0.0
    any_beta = False
    for h in book.priced:
        b = betas.get(h.symbol)
        if b is None:
            continue
        any_beta = True
        weighted += (h.market_value or 0.0) * b
        covered += abs(h.market_value or 0.0)
    gross_holdings = book.gross_invested
    has_value = total > 0
    return Exposure(
        long_value=long_value,
        short_value=short_value,
        gross=(long_value - short_value) / total if has_value else None,
        net=(long_value + short_value) / total if has_value else None,
        beta=weighted / total if any_beta and has_value else None,
        beta_coverage=covered / gross_holdings if gross_holdings else 0.0,
        benchmark=benchmark,
    )
