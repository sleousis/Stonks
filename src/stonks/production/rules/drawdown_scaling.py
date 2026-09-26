"""Drawdown-scaled risk budget (BL-27; Schwager/Benedict, Ghosh & Donadio).

The drawdown is measured from the running peak of the real portfolio's
equity curve (points up to ``as_of``) plus today's value. ``schedule`` is
a list of ``(drawdown threshold, size)`` levels, thresholds rising and
sizes falling; the default ``(5%, 1.0), (10%, 0.5), (15%, 0.25)`` halves
buys from a 10% drawdown and quarters them from 15%.

Hysteresis, so the size doesn't flap around a threshold: a level engages
as soon as the drawdown reaches its threshold, but is only released once
the drawdown falls below the *previous* level's threshold (the first
level's own threshold for the first level). So after a 10% drawdown buys
stay halved until the drawdown is back under 5%. The tick is stateless,
so the level is rebuilt by replaying the whole curve: the same curve
always gives the same size.

Only buys that open or grow a long position are scaled; sells and covers
pass. Off unless a schedule is set.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import scale_buys, settings_of

Schedule = tuple[tuple[float, float], ...]

DEFAULT_SCHEDULE: Schedule = ((0.05, 1.0), (0.10, 0.5), (0.15, 0.25))


class DrawdownScalingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``(drawdown threshold, size)`` levels; ``None`` switches the rule off.
    schedule: Schedule | None = None

    @field_validator("schedule")
    @classmethod
    def _ordered(cls, value: Schedule | None) -> Schedule | None:
        if value is None:
            return None
        if not value:
            raise ValueError("schedule needs at least one level")
        for dd, size in value:
            if not 0.0 < dd < 1.0:
                raise ValueError(f"drawdown threshold must be in (0, 1), got {dd}")
            if not 0.0 <= size <= 1.0:
                raise ValueError(f"size must be in [0, 1], got {size}")
        thresholds = [dd for dd, _ in value]
        sizes = [s for _, s in value]
        if any(b <= a for a, b in zip(thresholds, thresholds[1:], strict=False)):
            raise ValueError("schedule thresholds must strictly increase")
        if any(b > a for a, b in zip(sizes, sizes[1:], strict=False)):
            raise ValueError("schedule sizes must not increase with drawdown")
        return tuple((float(dd), float(s)) for dd, s in value)

    @property
    def active(self) -> bool:
        return self.schedule is not None


def drawdown_scale(values: Sequence[float], schedule: Schedule) -> tuple[float, float]:
    """``(size, current drawdown)`` after replaying ``values`` (oldest
    first) through ``schedule`` with hysteresis."""
    thresholds = [dd for dd, _ in schedule]
    level = -1  # no level engaged
    peak = 0.0
    dd = 0.0
    for value in values:
        peak = max(peak, value)
        dd = 1.0 - value / peak if peak > 0 else 0.0
        deepest = max((i for i, t in enumerate(thresholds) if dd >= t), default=-1)
        if deepest > level:
            level = deepest
        while level >= 0 and dd < thresholds[max(level - 1, 0)]:
            level -= 1
    return (schedule[level][1] if level >= 0 else 1.0), dd


@register_rule
class DrawdownScaling(RiskRule):
    name = "drawdown_scaling"
    order = 2
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: DrawdownScalingSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or settings.schedule is None:
            return list(orders), []
        curve = [v for d, v in ctx.equity_curve if ctx.as_of is None or d <= ctx.as_of]
        curve.append(ctx.portfolio.total_value(dict(ctx.prices)))
        scale, dd = drawdown_scale(curve, settings.schedule)
        reason = f"drawdown {dd:.2%} from peak {max(curve):.2f}; buys sized at {scale}"
        return scale_buys(orders, ctx, scale, self.name, reason)
