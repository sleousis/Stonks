"""Bar-interval value type.

``Interval`` names the duration of a single bar and is the first-class knob
that makes the Backtester, ingestion, and production tick work on any
time granularity — 1m through 5y.

Canonical codes:

- minutes: ``1m``, ``5m``, ``15m``, ``30m``
- hours:   ``1h``, ``4h``, ``6h``, ``12h``
- days:    ``1d``, ``3d``, ``5d``
- weeks:   ``1w``
- months:  ``1mo``, ``6mo``  (case-sensitive suffix to disambiguate from minutes)
- years:   ``1y``, ``5y``

``Interval.parse`` also accepts human aliases — ``1month``, ``6months``,
``1year``, ``5years`` — and normalizes them to the canonical short form.

Monthly and yearly durations are *approximate* in seconds (30 days and 365
days respectively). DuckDB interval arithmetic on those buckets honors the
real calendar, so query-time alignment is correct even when the in-memory
``seconds`` is a rounded approximation.

Usage::

    Interval.DAY_1
    Interval.parse("5m")
    Interval.parse("6mo")        == Interval.MONTH_6
    Interval.parse("6months")    == Interval.MONTH_6
    Interval.parse("4h").to_timedelta()  # timedelta(hours=4)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

_SECONDS_PER_DAY = 24 * 3600

# Unit → seconds map. Ordered longest-suffix-first for the parser.
_UNIT_SECONDS: dict[str, int] = {
    "mo": 30 * _SECONDS_PER_DAY,  # month  (approximate)
    "y": 365 * _SECONDS_PER_DAY,  # year   (approximate)
    "w": 7 * _SECONDS_PER_DAY,  # week
    "d": _SECONDS_PER_DAY,  # day
    "h": 3600,  # hour
    "m": 60,  # minute
}

# Human-readable aliases that map onto canonical units. We lowercase the
# input first, so the aliases are all-lowercase here.
_UNIT_ALIASES: dict[str, str] = {
    "month": "mo",
    "months": "mo",
    "year": "y",
    "years": "y",
    "min": "m",
    "mins": "m",
    "minute": "m",
    "minutes": "m",
    "hour": "h",
    "hours": "h",
    "day": "d",
    "days": "d",
    "week": "w",
    "weeks": "w",
}


@dataclass(frozen=True)
class Interval:
    code: str
    seconds: int

    # Well-known constants (populated below).
    MIN_1: ClassVar[Interval]
    MIN_5: ClassVar[Interval]
    MIN_15: ClassVar[Interval]
    MIN_30: ClassVar[Interval]
    HOUR_1: ClassVar[Interval]
    HOUR_4: ClassVar[Interval]
    HOUR_6: ClassVar[Interval]
    HOUR_12: ClassVar[Interval]
    DAY_1: ClassVar[Interval]
    DAY_3: ClassVar[Interval]
    DAY_5: ClassVar[Interval]
    WEEK_1: ClassVar[Interval]
    MONTH_1: ClassVar[Interval]
    MONTH_6: ClassVar[Interval]
    YEAR_1: ClassVar[Interval]
    YEAR_5: ClassVar[Interval]

    # Canonical set used by ``stonks ingest all-intervals`` and anywhere
    # the full grid is iterated.
    STANDARD: ClassVar[tuple[Interval, ...]]

    @classmethod
    def parse(cls, code: str) -> Interval:
        if not isinstance(code, str):
            raise TypeError(f"Interval code must be str, got {type(code).__name__}")
        s = code.strip().lower()
        if not s:
            raise ValueError(f"invalid Interval code: {code!r}")

        # Longest-match first so "month" isn't clipped to "m" + "onth".
        for alias in sorted(_UNIT_ALIASES, key=len, reverse=True):
            if s.endswith(alias):
                s = s[: -len(alias)] + _UNIT_ALIASES[alias]
                break

        # Extract the unit suffix (longest-match again).
        unit: str | None = None
        for candidate in sorted(_UNIT_SECONDS, key=len, reverse=True):
            if s.endswith(candidate):
                unit = candidate
                break
        if unit is None:
            raise ValueError(
                f"unknown unit in {code!r} "
                f"(expected one of {sorted(_UNIT_SECONDS, key=len, reverse=True)})"
            )
        num_part = s[: -len(unit)]
        if not num_part:
            raise ValueError(f"missing amount in Interval code: {code!r}")
        try:
            amount = int(num_part)
        except ValueError as exc:
            raise ValueError(f"non-integer amount in {code!r}") from exc
        if amount <= 0:
            raise ValueError(f"Interval amount must be positive, got {amount}")
        return cls(code=f"{amount}{unit}", seconds=amount * _UNIT_SECONDS[unit])

    def to_timedelta(self) -> timedelta:
        return timedelta(seconds=self.seconds)

    @property
    def is_intraday(self) -> bool:
        return self.seconds < _SECONDS_PER_DAY

    @property
    def duckdb_interval(self) -> str:
        """DuckDB INTERVAL literal equivalent, for use with ``time_bucket``.
        Preserves calendar semantics for months / years (DuckDB treats the
        literal as a calendar interval, not a fixed seconds count)."""
        unit = self.code.lstrip("0123456789")
        amount_str = self.code[: -len(unit)] or "1"
        expansion = {
            "m": "minute",
            "h": "hour",
            "d": "day",
            "w": "week",
            "mo": "month",
            "y": "year",
        }[unit]
        return f"INTERVAL '{amount_str} {expansion}s'"

    def __str__(self) -> str:
        return self.code


# ---- well-known constants --------------------------------------------------

Interval.MIN_1 = Interval.parse("1m")
Interval.MIN_5 = Interval.parse("5m")
Interval.MIN_15 = Interval.parse("15m")
Interval.MIN_30 = Interval.parse("30m")
Interval.HOUR_1 = Interval.parse("1h")
Interval.HOUR_4 = Interval.parse("4h")
Interval.HOUR_6 = Interval.parse("6h")
Interval.HOUR_12 = Interval.parse("12h")
Interval.DAY_1 = Interval.parse("1d")
Interval.DAY_3 = Interval.parse("3d")
Interval.DAY_5 = Interval.parse("5d")
Interval.WEEK_1 = Interval.parse("1w")
Interval.MONTH_1 = Interval.parse("1mo")
Interval.MONTH_6 = Interval.parse("6mo")
Interval.YEAR_1 = Interval.parse("1y")
Interval.YEAR_5 = Interval.parse("5y")

Interval.STANDARD = (
    Interval.MIN_1,
    Interval.MIN_5,
    Interval.HOUR_1,
    Interval.HOUR_4,
    Interval.HOUR_6,
    Interval.HOUR_12,
    Interval.DAY_1,
    Interval.DAY_3,
    Interval.DAY_5,
    Interval.WEEK_1,
    Interval.MONTH_1,
    Interval.MONTH_6,
    Interval.YEAR_1,
    Interval.YEAR_5,
)
