"""Bar-interval value type.

``Interval`` names the duration of a single bar and is the first-class knob
that makes the Backtester, ingestion, and production tick work on any
time granularity — 1m through 5y.

Canonical codes:

- minutes: ``1m``, ``5m``, ``15m``, ``30m``
- hours:   ``1h``, ``4h``, ``6h``, ``12h``
- days:    ``1d``, ``3d``, ``5d``
- weeks:   ``1w``
- months:  ``1mo``, ``6mo``  (``mo``, so a month is never read as a minute)
- years:   ``1y``, ``5y``

``Interval.parse`` ignores case and also accepts human aliases (``1month``,
``6months``, ``1year``, ``5years``), normalizing them to the canonical
short form. A bare upper-case ``M`` (``1M``) is refused: pandas and some
vendors mean a month by it. Equivalent codes collapse to the largest whole
unit (``60m`` is ``1h``, ``24h`` is ``1d``, ``7d`` is ``1w``, ``12mo`` is
``1y``), so one duration is always one ``bars`` series.

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

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
    "hr": "h",
    "hour": "h",
    "hours": "h",
    "day": "d",
    "days": "d",
    "week": "w",
    "weeks": "w",
}


# Unit -> (next larger unit, how many of this unit make one). Months and
# years are calendar units, so days never roll up into them.
_NEXT_UNIT: dict[str, tuple[str, int]] = {
    "m": ("h", 60),
    "h": ("d", 24),
    "d": ("w", 7),
    "mo": ("y", 12),
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
        if not isinstance(code, str):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise TypeError(f"Interval code must be str, got {type(code).__name__}")
        raw = code.strip()
        if raw.endswith("M") and raw[:-1].isdigit():
            raise ValueError(
                f"ambiguous Interval code {code!r}: use 'm' for minutes or 'mo' for months"
            )
        s = raw.lower()
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
        # the largest whole unit, so "60m" and "1h" are one series
        while unit in _NEXT_UNIT:
            bigger, factor = _NEXT_UNIT[unit]
            if amount % factor:
                break
            amount, unit = amount // factor, bigger
        return cls(code=f"{amount}{unit}", seconds=amount * _UNIT_SECONDS[unit])

    def to_timedelta(self) -> timedelta:
        return timedelta(seconds=self.seconds)

    @property
    def unit(self) -> str:
        """Canonical unit suffix: ``m``, ``h``, ``d``, ``w``, ``mo`` or ``y``."""
        return self.code.lstrip("0123456789")

    @property
    def amount(self) -> int:
        """Number of units in one bar (``5`` for ``5m``)."""
        return int(self.code[: -len(self.unit)])

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


# ---- bar visibility (RS-03, BL-49) -------------------------------------------
#
# A decision on the bar that starts at ``at`` is made at that bar's close,
# ``at + L`` (the "reach"). A bar of interval ``I`` stamped ``S`` is complete
# at ``S + I``, so the decision may see it when ``S + I <= at + L``. A row
# stamped with a calendar day (a filing, a macro print, a split) is known at
# the end of that day. The strategies' bar cache and the point-in-time lake
# (``stonks.store.pit``) both read these functions, so the rule lives once.

_ONE_DAY = timedelta(days=1)


def decision_reach(at: datetime, decision: Interval | None) -> datetime:
    """The close of the decision bar that starts at ``at``: ``at + L``.
    Without a known decision interval a midnight ``at`` is a daily decision
    (``L`` is one day) and any other time of day decides at ``at`` itself."""
    if decision is not None:
        return at + decision.to_timedelta()
    return at + (_ONE_DAY if at.time() == time.min else timedelta(0))


def _minus_months(when: datetime, months: int) -> datetime:
    """``when`` moved back ``months`` calendar months, the day clamped to
    the target month's length (like ``pandas.DateOffset``)."""
    total = when.year * 12 + (when.month - 1) - months
    year, month = divmod(total, 12)
    day = min(when.day, calendar.monthrange(year, month + 1)[1])
    return when.replace(year=year, month=month + 1, day=day)


def visible_cutoff(at: datetime, interval: Interval, decision: Interval | None) -> datetime:
    """The latest stamp of an ``interval`` bar that is complete at a decision
    on the bar starting at ``at`` (``at + L - I``). With no decision interval
    an intraday read keeps every bar stamped on or before ``at`` (the bar
    the decision stands on). Months and years use calendar months."""
    if decision is None and interval.is_intraday:
        return at
    reach = decision_reach(at, decision)
    if interval.unit in ("mo", "y"):
        return _minus_months(reach, interval.amount * (12 if interval.unit == "y" else 1))
    return reach - interval.to_timedelta()


def known_through(at: datetime, decision: Interval | None) -> date:
    """The last calendar day whose day-stamped rows are known at a decision
    on the bar starting at ``at`` (a day is known once it has ended)."""
    return (decision_reach(at, decision) - _ONE_DAY).date()
