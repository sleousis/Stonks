"""What the intraday risk rules read (roadmap 21.3.2).

The event driver (21.2) fills an :class:`IntradayContext` on each event and
puts it on ``RiskContext.intraday``. A context without one is a daily book,
and every intraday rule leaves its orders alone.

Every read stops at ``now``: a mark, a bar time or a sent order stamped
after the event is ignored, so a replay that hands the rules later data
cannot leak the future into a decision (P12).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from stonks.production.rules import RiskContext
from stonks.production.rules._common import book_value

__all__ = [
    "IntradayContext",
    "day_values",
    "intraday_of",
    "minute_suffix",
]


@dataclass(frozen=True)
class IntradayContext:
    """The live state of an intraday book at one event. Times are UTC."""

    #: The event time. Every rule reads the world as of this moment.
    now: datetime
    #: ``(time, marked book value)`` of the session so far, oldest first.
    equity_marks: Sequence[tuple[datetime, float]] = ()
    #: The timestamp of the latest bar per ticker.
    last_bar_at: Mapping[str, datetime] = field(default_factory=dict)
    #: When each order this book sent today went out (opens and closes).
    sent_at: Sequence[datetime] = ()
    #: The stream is stale or reconnecting: no fresh prices at all.
    stream_stale: bool = False


def intraday_of(ctx: RiskContext) -> IntradayContext | None:
    """The context's intraday state, or ``None`` for a daily book."""
    value: Any = ctx.intraday
    return value if isinstance(value, IntradayContext) else None


def day_values(
    ctx: RiskContext, intraday: IntradayContext, since: datetime | None = None
) -> list[float] | None:
    """The book's marked values today up to ``now`` (from ``since`` when
    given), oldest first, with the current value last. ``None`` when a
    holding has no mark: the value is unknown, so no rule reads a fake loss."""
    current = book_value(ctx)
    if current is None:
        return None
    day = intraday.now.date()
    values = [
        float(v)
        for t, v in intraday.equity_marks
        if t <= intraday.now and t.date() == day and (since is None or t >= since)
    ]
    return [*values, current]


def minute_suffix(intraday: IntradayContext) -> str:
    """``HHMM`` of the event, so orders a rule adds are unique per minute
    (``make_client_id`` is unique per day only)."""
    return intraday.now.strftime("%H%M")
