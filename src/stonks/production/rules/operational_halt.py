"""Operational halt (BL-28; Part 7: a stale feed must not open new risk).

When the newest bar across the context's history is more than
``max_bar_age_days`` calendar days older than ``as_of``, the data feed
has stopped and every buy that opens or grows a long position is dropped.
Sells, exits and covers pass. Off unless ``max_bar_age_days`` is set.

This is the pure half, which also runs in backtests. The live half is the
global ``operational`` row in ``risk_halts``: ``production.halts``
opens it when ``stonks health`` finds stale data or a stuck run, and the
``risk_halts`` trade gate blocks buys while it is open.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import history, scale_opens, settings_of


class OperationalHaltSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Calendar days the newest bar may lag ``as_of``; ``None`` is off.
    max_bar_age_days: int | None = Field(default=None, ge=1)

    @property
    def active(self) -> bool:
        return self.max_bar_age_days is not None


@register_rule
class OperationalHalt(RiskRule):
    name = "operational_halt"
    order = 5
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: OperationalHaltSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or settings.max_bar_age_days is None or ctx.as_of is None:
            return list(orders), []
        newest: pd.Timestamp | None = None
        for ticker in ctx.history:
            frame = history(ctx, ticker)
            if frame is not None:
                last = pd.Timestamp(frame.index[-1])
                newest = last if newest is None or last > newest else newest
        if newest is None:
            return list(orders), []
        age = (pd.Timestamp(ctx.as_of) - newest.normalize()).days
        if age <= settings.max_bar_age_days:
            return list(orders), []
        reason = (
            f"operational halt: newest bar is {age} days old (limit {settings.max_bar_age_days})"
        )
        return scale_opens(orders, ctx, 0.0, self.name, reason)
