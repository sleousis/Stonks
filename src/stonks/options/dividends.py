"""Dividend forecasts for option pricing (roadmap 17.7).

A :class:`DividendForecast` turns the dividends expected before a
contract's expiry into the continuous dividend yield the pricing models
take. Discrete cash dividends become the yield ``q`` with
``S exp(-q T) = S - PV(dividends)``, so a European price equals the
escrowed dividend model (QuantLib's ``AnalyticDividendEuropeanEngine``).

:class:`KnownDividendForecast` reads the ``dividends`` table and only uses
what was known on the pricing day (P12):

- a future ex-date counts when its ``declaration_date`` is on or before
  the pricing day. A row with no declaration date is not known before its
  ex-date, so it never counts as a future dividend;
- after the last known ex-date (or from the pricing day when none is
  declared) a trailing yield fills the rest of the tenor: the cash paid
  over the last ``trailing_days`` divided by the spot.

Ex-dates strictly after the pricing day and on or before the expiry
count. A dividend going ex on the pricing day is already in the spot.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from stonks.options.rates import RateCurve

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

#: The present value of dividends is capped just below the spot.
_MAX_PV_SHARE = 0.999


@dataclass(frozen=True)
class DividendRecord:
    ex_date: date
    amount: float
    declaration_date: date | None = None


class DividendForecast(ABC):
    """The dividend yield of an underlying between a pricing day and an expiry."""

    @abstractmethod
    def dividend_yield(
        self, underlying: str, as_of: date, expiry: date, spot: float, curve: RateCurve
    ) -> float:
        """Continuous yield per year over ``(as_of, expiry]``."""


class NoDividends(DividendForecast):
    def dividend_yield(
        self, underlying: str, as_of: date, expiry: date, spot: float, curve: RateCurve
    ) -> float:
        return 0.0


class FlatDividendYield(DividendForecast):
    def __init__(self, dividend_yield: float = 0.0) -> None:
        if not math.isfinite(dividend_yield):
            raise ValueError(f"dividend_yield must be finite, got {dividend_yield}")
        self.value = dividend_yield

    def dividend_yield(
        self, underlying: str, as_of: date, expiry: date, spot: float, curve: RateCurve
    ) -> float:
        return self.value


def _day(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT
            return None
    except TypeError:  # pragma: no cover - defensive
        return None
    if hasattr(value, "date") and callable(value.date):
        return value.date()
    return value if isinstance(value, date) else None


class KnownDividendForecast(DividendForecast):
    """Declared future dividends plus a trailing yield (see the module doc)."""

    def __init__(
        self,
        records: Mapping[str, Sequence[DividendRecord]],
        *,
        trailing_days: int = 365,
    ) -> None:
        if trailing_days < 0:
            raise ValueError(f"trailing_days must be non-negative, got {trailing_days}")
        self._records = {u: sorted(rs, key=lambda r: r.ex_date) for u, rs in records.items()}
        self._trailing_days = trailing_days

    @classmethod
    def from_lake(
        cls, lake: DuckDBLake, underlyings: Sequence[str], *, trailing_days: int = 365
    ) -> KnownDividendForecast:
        records: dict[str, list[DividendRecord]] = {}
        for ticker in underlyings:
            frame = lake.get_dividends(ticker)
            rows: list[DividendRecord] = []
            for ex, amount, declared in zip(
                frame["ex_date"], frame["amount"], frame["declaration_date"], strict=True
            ):
                ex_day = _day(ex)
                if ex_day is None or amount is None or not math.isfinite(float(amount)):
                    continue
                rows.append(DividendRecord(ex_day, float(amount), _day(declared)))
            records[ticker] = rows
        return cls(records, trailing_days=trailing_days)

    def known_dividends(
        self, underlying: str, as_of: date, expiry: date
    ) -> list[tuple[date, float]]:
        """Dividends declared by ``as_of`` going ex in ``(as_of, expiry]``."""
        return [
            (r.ex_date, r.amount)
            for r in self._records.get(underlying, ())
            if as_of < r.ex_date <= expiry
            and r.declaration_date is not None
            and r.declaration_date <= as_of
        ]

    def trailing_cash(self, underlying: str, as_of: date) -> float:
        """Cash per share gone ex over the last ``trailing_days``."""
        start = as_of - timedelta(days=self._trailing_days)
        return sum(
            r.amount for r in self._records.get(underlying, ()) if start < r.ex_date <= as_of
        )

    def dividend_yield(
        self, underlying: str, as_of: date, expiry: date, spot: float, curve: RateCurve
    ) -> float:
        total = (expiry - as_of).days / 365.0
        if total <= 0 or spot <= 0:
            return 0.0
        known = self.known_dividends(underlying, as_of, expiry)
        pv = 0.0
        for ex, amount in known:
            t = (ex - as_of).days / 365.0
            pv += amount * curve.discount(as_of, t)
        pv = min(pv, spot * _MAX_PV_SHARE)
        q = -math.log1p(-pv / spot) / total if pv > 0 else 0.0
        trailing = self.trailing_cash(underlying, as_of)
        if trailing > 0:
            last = (known[-1][0] - as_of).days / 365.0 if known else 0.0
            q += math.log1p(trailing / spot) * max(total - last, 0.0) / total
        return q
