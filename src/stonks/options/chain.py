"""Option quotes and chain snapshots as domain values (roadmap 17.1).

:class:`OptionQuote` is one contract's end-of-day quote. A
:class:`ChainSnapshot` is every quote of one underlying on one day, the
input the leg selector and the options backtest read.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date

from stonks.core.options import OptionContract


@dataclass(frozen=True)
class OptionQuote:
    contract: OptionContract
    as_of: date
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    underlying_price: float | None = None
    #: The vendor's implied vol and Greeks, when it sent them.
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    @property
    def two_sided(self) -> bool:
        """A usable market: a positive bid and an ask at or above it."""
        return (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask >= self.bid
            and math.isfinite(self.ask)
        )

    @property
    def mid(self) -> float | None:
        if not self.two_sided:
            return None
        return (self.bid + self.ask) / 2.0  # type: ignore[operator]

    @property
    def half_spread(self) -> float | None:
        if not self.two_sided:
            return None
        return (self.ask - self.bid) / 2.0  # type: ignore[operator]

    @property
    def spread_pct(self) -> float | None:
        """Bid-ask spread over the mid."""
        mid, half = self.mid, self.half_spread
        if mid is None or half is None or mid <= 0:
            return None
        return 2.0 * half / mid

    @property
    def mark(self) -> float | None:
        """The mid, else the last trade: the value a position is marked at."""
        mid = self.mid
        if mid is not None:
            return mid
        return self.last


@dataclass(frozen=True)
class ChainSnapshot:
    underlying: str
    as_of: date
    quotes: tuple[OptionQuote, ...] = field(default_factory=tuple[OptionQuote, ...])
    #: The underlying's close that day, when known.
    spot: float | None = None

    def __iter__(self) -> Iterator[OptionQuote]:
        return iter(self.quotes)

    def __len__(self) -> int:
        return len(self.quotes)

    def by_id(self) -> dict[str, OptionQuote]:
        return {q.contract_id: q for q in self.quotes}

    def expiries(self) -> list[date]:
        return sorted({q.contract.expiry for q in self.quotes})

    def filter(
        self,
        *,
        right: str | None = None,
        expiry: date | None = None,
        two_sided: bool = False,
    ) -> list[OptionQuote]:
        return [
            q
            for q in self.quotes
            if (right is None or q.contract.right == right)
            and (expiry is None or q.contract.expiry == expiry)
            and (not two_sided or q.two_sided)
        ]


def snapshots(quotes: Iterable[OptionQuote]) -> dict[tuple[str, date], ChainSnapshot]:
    """Group quotes into one snapshot per ``(underlying, day)``."""
    grouped: dict[tuple[str, date], list[OptionQuote]] = {}
    for q in quotes:
        grouped.setdefault((q.contract.underlying, q.as_of), []).append(q)
    out: dict[tuple[str, date], ChainSnapshot] = {}
    for (underlying, day), qs in grouped.items():
        spots = [q.underlying_price for q in qs if q.underlying_price]
        out[(underlying, day)] = ChainSnapshot(
            underlying, day, tuple(qs), spot=spots[0] if spots else None
        )
    return out
