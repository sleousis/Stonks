"""FX rates as an in-memory lookup built from the lake's ``fx_rates``."""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from datetime import date
from typing import Any

#: Minor-unit codes some exchanges quote in: (major currency, units per major).
MINOR_UNITS: dict[str, tuple[str, float]] = {
    "GBX": ("GBP", 100.0),  # London pence
    "ZAC": ("ZAR", 100.0),  # Johannesburg cents
    "ILA": ("ILS", 100.0),  # Tel Aviv agorot
}

#: The currency crosses go through when no direct or inverse pair exists.
CROSS = "USD"

RateRow = tuple[str, str, date, float]


class FxRateMissing(LookupError):
    """No stored rate converts ``source`` to ``target`` on ``day``."""

    def __init__(self, source: str, target: str, day: date) -> None:
        super().__init__(f"no FX rate from {source} to {target} on or before {day.isoformat()}")
        self.source = source
        self.target = target
        self.day = day


def normalize_currency(code: str) -> tuple[str, float]:
    """``(ISO code, factor)``: an amount in ``code`` times ``factor`` is in
    the ISO code. ``GBX`` (and ``GBp``) is GBP / 100."""
    raw = code.strip()
    if raw == "GBp":
        return "GBP", 0.01
    upper = raw.upper()
    if upper in MINOR_UNITS:
        major, per = MINOR_UNITS[upper]
        return major, 1.0 / per
    return upper, 1.0


class FxRates:
    """Rates per pair, each a date-sorted series. Pure: no lake access."""

    def __init__(self, rows: Iterable[RateRow]) -> None:
        series: dict[tuple[str, str], dict[date, float]] = {}
        for base, quote, day, rate in rows:
            if rate <= 0:
                continue
            series.setdefault((base.upper(), quote.upper()), {})[day] = float(rate)
        self._days: dict[tuple[str, str], list[date]] = {}
        self._values: dict[tuple[str, str], list[float]] = {}
        for pair, points in series.items():
            days = sorted(points)
            self._days[pair] = days
            self._values[pair] = [points[d] for d in days]

    def _direct(self, base: str, quote: str, day: date) -> float | None:
        days = self._days.get((base, quote))
        if not days:
            return None
        i = bisect.bisect_right(days, day) - 1
        return self._values[(base, quote)][i] if i >= 0 else None

    def _pair(self, base: str, quote: str, day: date) -> float | None:
        if base == quote:
            return 1.0
        direct = self._direct(base, quote, day)
        if direct is not None:
            return direct
        inverse = self._direct(quote, base, day)
        return 1.0 / inverse if inverse is not None else None

    def rate(self, source: str, target: str, day: date) -> float | None:
        """Units of ``target`` per one unit of ``source`` on ``day``, or
        ``None`` when no stored rate gives it."""
        src, src_factor = normalize_currency(source)
        dst, dst_factor = normalize_currency(target)
        core = self._pair(src, dst, day)
        if core is None and CROSS not in (src, dst):
            left, right = self._pair(src, CROSS, day), self._pair(CROSS, dst, day)
            if left is not None and right is not None:
                core = left * right
        if core is None:
            return None
        return src_factor * core / dst_factor

    def convert(self, amount: float, source: str, target: str, day: date) -> float | None:
        """``amount`` in ``source`` as ``target`` on ``day``, or ``None``."""
        rate = self.rate(source, target, day)
        return None if rate is None else amount * rate

    def convert_or_raise(self, amount: float, source: str, target: str, day: date) -> float:
        value = self.convert(amount, source, target, day)
        if value is None:
            raise FxRateMissing(source, target, day)
        return value


def load_fx_rates(
    lake: Any, currencies: Iterable[str] | None = None, *, end: date | None = None
) -> FxRates:
    """Every stored rate (optionally only pairs among ``currencies`` plus
    USD for crosses, and days up to ``end``) as :class:`FxRates`."""
    codes = None
    if currencies is not None:
        codes = {normalize_currency(c)[0] for c in currencies} | {CROSS}
    df = lake.get_fx_rates(codes, end=end)
    return FxRates(
        (str(r.base_currency), str(r.quote_currency), r.observation_date, float(r.rate))
        for r in df.itertuples(index=False)
    )


def sum_in_base(
    amounts: Iterable[tuple[float, str | None]],
    base: str,
    fx: FxRates | None,
    day: date,
) -> tuple[float | None, list[str]]:
    """``(total, missing)``: every ``(amount, currency)`` converted to
    ``base`` on ``day`` and summed. A ``None`` currency is taken as
    ``base``. When a currency has no rate the total is ``None`` and
    ``missing`` names it (never guessed)."""
    rates = fx if fx is not None else FxRates([])
    total = 0.0
    missing: set[str] = set()
    for amount, currency in amounts:
        ccy = currency or base
        value = amount if ccy == base else rates.convert(amount, ccy, base, day)
        if value is None:
            missing.add(ccy)
        else:
            total += value
    return (None if missing else total), sorted(missing)
