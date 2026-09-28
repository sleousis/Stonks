"""A portfolio's daily values in its base currency (roadmap 20.5).

Each snapshot is revalued as cash (kept in the base currency) plus every
holding at that day's close (the latest close on or before the day)
converted at that day's FX rate. When no holding trades in a foreign
currency the snapshot's own ``total_value`` is kept, so a single-currency
book reads exactly as before. A holding with no close on or before the day
counts as 0, like the tick's own valuation of an unpriced holding."""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from stonks.fx.rates import FxRates


@dataclass(frozen=True)
class SnapshotPoint:
    day: date
    cash: float
    positions: Mapping[str, float]
    total_value: float


class CloseSeries:
    """Closes per ticker, looked up as the latest on or before a day."""

    def __init__(self, closes: Mapping[str, Sequence[tuple[date, float]]]) -> None:
        self._days = {t: [d for d, _ in sorted(v)] for t, v in closes.items()}
        self._values = {t: [c for _, c in sorted(v)] for t, v in closes.items()}

    def close(self, ticker: str, day: date) -> float | None:
        days = self._days.get(ticker)
        if not days:
            return None
        i = bisect.bisect_right(days, day) - 1
        return self._values[ticker][i] if i >= 0 else None


def values_in_base(
    snapshots: Sequence[SnapshotPoint],
    closes: CloseSeries,
    currencies: Mapping[str, str],
    base: str,
    fx: FxRates,
) -> tuple[list[tuple[date, float]] | None, list[str]]:
    """``(points, missing)``: one ``(day, value in base)`` per snapshot, or
    ``None`` and the currencies without a rate on some day."""
    foreign = {t for s in snapshots for t in s.positions if currencies.get(t, base) != base}
    if not foreign:
        return [(s.day, s.total_value) for s in snapshots], []
    points: list[tuple[date, float]] = []
    missing: set[str] = set()
    for snap in snapshots:
        value = snap.cash
        for ticker, qty in snap.positions.items():
            price = closes.close(ticker, snap.day)
            if price is None:
                continue
            amount = qty * price
            ccy = currencies.get(ticker, base)
            converted = amount if ccy == base else fx.convert(amount, ccy, base, snap.day)
            if converted is None:
                missing.add(ccy)
                continue
            value += converted
        points.append((snap.day, value))
    return (None if missing else points), sorted(missing)
