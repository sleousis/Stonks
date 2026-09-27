"""Time-weighted and money-weighted returns (roadmap 20.5).

A plain change in value counts a deposit as profit. These returns take the
external cash flows out:

- **Flows** are deposits (positive) and withdrawals (negative) only.
  Dividends, interest and fees stay inside the return: they are what the
  book earned or paid, not money a person moved.
- **Convention:** a flow lands at the start of its day, so that day's value
  already holds it. The sub-period return of day ``t`` is
  ``V_t / (V_{t-1} + F_t) - 1``. A flow dated before the first value, or on
  it, is part of the starting capital and not a flow inside the range.
- **Time-weighted (TWR):** the sub-period returns chained,
  ``prod(1 + r_t) - 1``. It measures the investment decisions, whatever the
  timing and size of the flows.
- **Money-weighted (MWR):** the annualized internal rate of return (XIRR,
  365-day years) of the starting value, the flows and the ending value. It
  measures what the person's money earned, timing included.

Both are pure: values and flows in, a number (or ``None``) out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from scipy.optimize import brentq

#: Largest and smallest annual rate the XIRR search looks at.
_RATE_LOW, _RATE_HIGH = -0.9999, 1e6


@dataclass(frozen=True)
class Flow:
    day: date
    #: Positive: money in (deposit). Negative: money out (withdrawal).
    amount: float


def flows_by_day(flows: Sequence[Flow]) -> dict[date, float]:
    out: dict[date, float] = {}
    for f in flows:
        out[f.day] = out.get(f.day, 0.0) + f.amount
    return out


def net_flows(points: Sequence[tuple[date, float]], flows: Sequence[Flow]) -> float:
    """Sum of the flows strictly after the first value and up to the last."""
    if not points:
        return 0.0
    start, end = points[0][0], points[-1][0]
    return sum(f.amount for f in flows if start < f.day <= end)


def twr(points: Sequence[tuple[date, float]], flows: Sequence[Flow]) -> float | None:
    """Time-weighted return over ``points`` (``(day, value)``, oldest first).
    ``None`` with fewer than two values or when a base is not positive."""
    if len(points) < 2:
        return None
    by_day = flows_by_day(flows)
    growth = 1.0
    prev_day, prev_value = points[0]
    for day, value in points[1:]:
        # every flow between the previous value and this one lands today
        flow = sum(a for d, a in by_day.items() if prev_day < d <= day)
        base = prev_value + flow
        if base <= 0:
            return None
        growth *= value / base
        prev_day, prev_value = day, value
    return growth - 1.0


def mwr(points: Sequence[tuple[date, float]], flows: Sequence[Flow]) -> float | None:
    """Annualized money-weighted return (XIRR). ``None`` with fewer than two
    values, a range shorter than a day, a non-positive start, or no root."""
    if len(points) < 2:
        return None
    (start, v0), (end, v1) = points[0], points[-1]
    if v0 <= 0 or end <= start:
        return None
    cash: list[tuple[float, float]] = [(0.0, -v0)]
    for f in flows:
        if start < f.day <= end:
            cash.append(((f.day - start).days / 365.0, -f.amount))
    cash.append(((end - start).days / 365.0, v1))

    def npv(rate: float) -> float:
        return sum(amount / (1.0 + rate) ** t for t, amount in cash)

    try:
        low, high = npv(_RATE_LOW), npv(_RATE_HIGH)
    except (OverflowError, ZeroDivisionError):
        return None
    if low * high > 0:
        return None
    try:
        root: Any = brentq(npv, _RATE_LOW, _RATE_HIGH, xtol=1e-12, maxiter=500)
        return float(root)
    except (ValueError, RuntimeError, OverflowError):  # pragma: no cover - guarded above
        return None


def twr_since(
    points: Sequence[tuple[date, float]], flows: Sequence[Flow], start: date | None
) -> float | None:
    """TWR from the last value on or before ``start`` to the latest value."""
    if start is None:
        return None
    window = [p for p in points if p[0] >= start]
    before = [p for p in points if p[0] <= start]
    if before and (not window or window[0][0] != before[-1][0]):
        window = [before[-1], *window]
    return twr(window, flows)


def as_flows(amounts: Mapping[date, float]) -> list[Flow]:
    return [Flow(d, a) for d, a in sorted(amounts.items())]
