"""What the manual discipline rule reads (roadmap 23.4).

``production.manual`` fills a :class:`ManualContext` for the one order a
person places by hand and puts it on ``RiskContext.manual``. A context
without one is a strategy's order, and the rule leaves it alone. Every
figure stops at ``now`` (P12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stonks.production.rules import RiskContext

__all__ = ["ManualContext", "manual_of"]


@dataclass(frozen=True)
class ManualContext:
    """The book's manual trading so far today. Times are UTC."""

    now: datetime
    #: The book trades real money at a broker.
    live: bool = False
    #: The order carries a protective stop.
    has_stop: bool = False
    #: Manual entries placed today (not rejected), before this one.
    entries_today: int = 0
    #: Realised P&L of today's manual exits (a loss is negative).
    pnl_today: float = 0.0
    #: When the latest manual exit at a loss filled.
    last_losing_exit_at: datetime | None = None


def manual_of(ctx: RiskContext) -> ManualContext | None:
    """The context's manual state, or ``None`` for a strategy's orders."""
    value: Any = ctx.manual
    return value if isinstance(value, ManualContext) else None
