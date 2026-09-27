"""Intraday drawdown scaling (roadmap 21.3.2, P27).

``drawdown_scaling`` for one session: the drawdown is measured from the
highest mark of the day (UTC date of the event) to the current marked
value, and ``schedule`` sizes opening orders by it, with the same
hysteresis (a level engages at its threshold and is released only below
the previous level's). The day's marks are replayed on every event, so the
same marks always give the same size.

Only opening orders shrink. Sells of longs and covers pass untouched.
Acts only on an intraday book. Off unless a schedule is set.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import scale_opens, settings_of
from stonks.production.rules._intraday import day_values, intraday_of
from stonks.production.rules.drawdown_scaling import DrawdownScalingSettings, drawdown_scale

__all__ = ["IntradayDrawdown", "IntradayDrawdownSettings"]


class IntradayDrawdownSettings(DrawdownScalingSettings):
    """``(drawdown from the day's high, size)`` levels; ``None`` is off."""


@register_rule
class IntradayDrawdown(RiskRule):
    name = "intraday_drawdown"
    order = 4
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: IntradayDrawdownSettings | None = settings_of(ctx.policy, self.name)
        intraday = intraday_of(ctx)
        if settings is None or settings.schedule is None or intraday is None:
            return list(orders), []
        values = day_values(ctx, intraday)
        if values is None:
            return list(orders), []
        scale, dd = drawdown_scale(values, settings.schedule)
        reason = (
            f"intraday drawdown {dd:.2%} from the day's high {max(values):.2f}; "
            f"opening orders sized at {scale}"
        )
        return scale_opens(orders, ctx, scale, self.name, reason)
