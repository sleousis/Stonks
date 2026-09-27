"""Risk-free rates for option pricing (roadmap 17.7).

A :class:`RateCurve` answers one question: the continuously compounded
zero rate from a pricing day to a time ``t`` years ahead. Pricing asks it
for the rate at each contract's expiry, so a 1-week and a 2-year option
discount at different rates.

- :class:`FlatRateCurve` is one configured rate at every tenor.
- :class:`TreasuryRateCurve` reads constant-maturity Treasury yields from
  ``bond_yield_history`` (``US3M.GBOND``, ``US10Y.GBOND``, ...). For each
  tenor it takes the latest yield dated on or before the pricing day (P12),
  drops a yield older than ``max_age_days``, converts the bond-equivalent
  percent to a continuous rate and interpolates linearly in time between
  tenors, flat beyond the ends. With no usable yield it returns the flat
  ``fallback_rate`` and logs why, once per day.
"""

from __future__ import annotations

import bisect
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, Field, field_validator

from stonks.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.options.rates")

#: Constant-maturity US Treasury yields by ticker, tenor in years.
DEFAULT_TREASURY_TICKERS: dict[str, float] = {
    "US1M.GBOND": 1 / 12,
    "US3M.GBOND": 0.25,
    "US6M.GBOND": 0.5,
    "US1Y.GBOND": 1.0,
    "US2Y.GBOND": 2.0,
    "US3Y.GBOND": 3.0,
    "US5Y.GBOND": 5.0,
    "US7Y.GBOND": 7.0,
    "US10Y.GBOND": 10.0,
    "US20Y.GBOND": 20.0,
    "US30Y.GBOND": 30.0,
}


class RateCurveSettings(BaseModel):
    """Where the Treasury curve comes from and when to give up on it."""

    #: ``bond_yield_history`` ticker -> tenor in years.
    tickers: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_TREASURY_TICKERS))
    #: A yield older than this many calendar days is not used.
    max_age_days: int = Field(default=10, ge=0)
    #: The flat rate used when no yield is usable (continuous, per year).
    fallback_rate: float = 0.0

    @field_validator("tickers")
    @classmethod
    def _positive_tenors(cls, value: dict[str, float]) -> dict[str, float]:
        for ticker, tenor in value.items():
            if not (math.isfinite(tenor) and tenor > 0):
                raise ValueError(f"tenor of {ticker} must be positive, got {tenor}")
        return value


def bond_equivalent_to_continuous(yield_pct: float) -> float:
    """A bond-equivalent (semi-annual) yield in percent as a continuously
    compounded rate: ``2 ln(1 + y / 200)``."""
    return 2.0 * math.log1p(yield_pct / 200.0)


class RateCurve(ABC):
    """Continuously compounded zero rates seen on a pricing day."""

    @abstractmethod
    def zero_rate(self, as_of: date, time: float) -> float:
        """The zero rate from ``as_of`` to ``time`` years later."""

    def discount(self, as_of: date, time: float) -> float:
        return math.exp(-self.zero_rate(as_of, time) * time)


class FlatRateCurve(RateCurve):
    def __init__(self, rate: float = 0.0) -> None:
        if not math.isfinite(rate):
            raise ValueError(f"rate must be finite, got {rate}")
        self.rate = rate

    def zero_rate(self, as_of: date, time: float) -> float:
        return self.rate

    def __repr__(self) -> str:
        return f"FlatRateCurve({self.rate})"


class TreasuryRateCurve(RateCurve):
    """Treasury zero rates known on the pricing day (see the module doc)."""

    def __init__(
        self,
        points: Mapping[float, Sequence[tuple[date, float]]],
        *,
        settings: RateCurveSettings | None = None,
    ) -> None:
        """``points`` maps a tenor in years to ``(date, yield percent)``
        observations in any order."""
        self._settings = settings or RateCurveSettings()
        self._series: list[tuple[float, list[date], list[float]]] = []
        for tenor in sorted(points):
            rows = sorted((d, y) for d, y in points[tenor] if y is not None and math.isfinite(y))
            if rows:
                self._series.append((tenor, [d for d, _ in rows], [y for _, y in rows]))
        self._knots: dict[date, tuple[np.ndarray, np.ndarray] | str] = {}
        self._logged: set[date] = set()

    @classmethod
    def from_lake(
        cls, lake: DuckDBLake, settings: RateCurveSettings | None = None
    ) -> TreasuryRateCurve:
        """Every stored yield of the configured tickers. The curve itself
        filters by pricing day, so one load serves a whole backtest."""
        settings = settings or RateCurveSettings()
        points: dict[float, list[tuple[date, float]]] = {}
        for ticker, tenor in settings.tickers.items():
            frame = lake.get_bond_yields(ticker)
            rows = [
                (d, float(y))
                for d, y in zip(frame["date"], frame["yield_to_maturity"], strict=True)
                if y is not None and not (isinstance(y, float) and math.isnan(y))
            ]
            if rows:
                points.setdefault(tenor, []).extend(rows)
        return cls(points, settings=settings)

    def _build(self, as_of: date) -> tuple[np.ndarray, np.ndarray] | str:
        if not self._series:
            return "no treasury yields known"
        oldest = as_of - timedelta(days=self._settings.max_age_days)
        tenors: list[float] = []
        rates: list[float] = []
        stale = 0
        for tenor, dates, yields in self._series:
            i = bisect.bisect_right(dates, as_of) - 1
            if i < 0:
                continue
            if dates[i] < oldest:
                stale += 1
                continue
            tenors.append(tenor)
            rates.append(bond_equivalent_to_continuous(yields[i]))
        if tenors:
            return np.asarray(tenors), np.asarray(rates)
        if stale:
            return f"treasury yields stale (older than {self._settings.max_age_days} days)"
        return "no treasury yields known"

    def _knots_for(self, as_of: date) -> tuple[np.ndarray, np.ndarray] | str:
        knots = self._knots.get(as_of)
        if knots is None:
            knots = self._knots[as_of] = self._build(as_of)
        return knots

    def fallback_reason(self, as_of: date) -> str | None:
        """Why the curve falls back to the flat rate on ``as_of``, or
        ``None`` when it has yields."""
        knots = self._knots_for(as_of)
        return knots if isinstance(knots, str) else None

    def zero_rate(self, as_of: date, time: float) -> float:
        knots = self._knots_for(as_of)
        if isinstance(knots, str):
            if as_of not in self._logged:
                self._logged.add(as_of)
                _log.warning(
                    "options.rate_curve.flat_fallback",
                    as_of=str(as_of),
                    reason=knots,
                    rate=self._settings.fallback_rate,
                )
            return self._settings.fallback_rate
        tenors, rates = knots
        return float(np.interp(time, tenors, rates))
