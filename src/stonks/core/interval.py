"""Bar-interval value type.

``Interval`` names the duration of a single bar (1m, 5m, 15m, 1h, 4h, 12h,
1d, 1w, …) and is the first-class knob that makes the Backtester and the
production tick work on any time granularity, not just daily.

Each interval has a canonical string code (``Interval.code``) stored
alongside the data in the ``bars`` table and a ``timedelta``.

Usage::

    Interval.DAY_1
    Interval.parse("5m")
    Interval.parse("4h").to_timedelta()      # timedelta(hours=4)
    Interval.parse("15m") == Interval.MIN_15 # True
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

_UNIT_SECONDS: dict[str, int] = {
    "m": 60,
    "h": 3600,
    "d": 24 * 3600,
    "w": 7 * 24 * 3600,
}


@dataclass(frozen=True)
class Interval:
    code: str        # canonical, always lowercase — e.g. "5m", "4h", "1d"
    seconds: int     # duration in seconds

    # well-known constants, populated at module import time
    MIN_1: ClassVar[Interval]
    MIN_5: ClassVar[Interval]
    MIN_15: ClassVar[Interval]
    MIN_30: ClassVar[Interval]
    HOUR_1: ClassVar[Interval]
    HOUR_4: ClassVar[Interval]
    HOUR_12: ClassVar[Interval]
    DAY_1: ClassVar[Interval]
    WEEK_1: ClassVar[Interval]

    @classmethod
    def parse(cls, code: str) -> Interval:
        if not isinstance(code, str):
            raise TypeError(f"Interval code must be str, got {type(code).__name__}")
        s = code.strip().lower()
        if not s or not s[-1].isalpha():
            raise ValueError(f"invalid Interval code: {code!r}")
        unit = s[-1]
        if unit not in _UNIT_SECONDS:
            raise ValueError(
                f"unknown unit {unit!r} in {code!r} (expected one of {sorted(_UNIT_SECONDS)})"
            )
        num_part = s[:-1]
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
        return self.seconds < _UNIT_SECONDS["d"]

    def __str__(self) -> str:
        return self.code


# ---- well-known constants --------------------------------------------------

Interval.MIN_1 = Interval.parse("1m")
Interval.MIN_5 = Interval.parse("5m")
Interval.MIN_15 = Interval.parse("15m")
Interval.MIN_30 = Interval.parse("30m")
Interval.HOUR_1 = Interval.parse("1h")
Interval.HOUR_4 = Interval.parse("4h")
Interval.HOUR_12 = Interval.parse("12h")
Interval.DAY_1 = Interval.parse("1d")
Interval.WEEK_1 = Interval.parse("1w")
