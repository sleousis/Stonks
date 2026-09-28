"""Discipline rules for manual orders (roadmap 23.4). Off by default.

A person trading by hand can set limits on themselves, under
``[production.risk.rules.manual_discipline]`` or a portfolio override
(tighten only). Each one refuses a new manual entry:

- ``require_stop_live``: an entry in a real-money book needs a protective
  stop on the ticket;
- ``cooldown_minutes``: no new entry for this long after a manual exit at a
  loss (a revenge trade);
- ``max_entries_per_day``: at most this many manual entries a day (UTC);
- ``max_daily_loss``: no new entry once today's manual exits lost this
  much, in the book's currency.

Exits always pass (P28): a limit on yourself must never trap you in a
position. Acts only on an order placed by hand (``ctx.manual``), never on
a strategy's orders.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import (
    EPS,
    RiskAdjustment,
    RiskContext,
    RiskRule,
    register_rule,
)
from stonks.production.rules._common import settings_of
from stonks.production.rules._manual import ManualContext, manual_of


class ManualDisciplineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    require_stop_live: bool = True
    cooldown_minutes: int | None = Field(default=None, ge=1, le=10_080)
    max_entries_per_day: int | None = Field(default=None, ge=1, le=1_000)
    max_daily_loss: float | None = Field(default=None, gt=0.0)

    @property
    def active(self) -> bool:
        return self.enabled


def _opens(order: Order, positions: dict[str, float]) -> bool:
    """Whether ``order`` opens or grows a position (not an exit)."""
    held = float(positions.get(order.ticker, 0.0))
    if order.side == "sell":
        return not (held > EPS and order.quantity <= held + EPS)
    return not (held < -EPS and order.quantity <= -held + EPS)


def refusal(settings: ManualDisciplineSettings, manual: ManualContext) -> str | None:
    """Why a new manual entry is refused now, or ``None``."""
    if settings.require_stop_live and manual.live and not manual.has_stop:
        return "a manual entry in a real-money book needs a protective stop"
    last = manual.last_losing_exit_at
    if settings.cooldown_minutes is not None and last is not None:
        until = last + timedelta(minutes=settings.cooldown_minutes)
        if manual.now < until:
            return (
                f"cooling down after a losing manual exit at {last:%H:%M} UTC; "
                f"new entries from {until:%H:%M} UTC"
            )
    cap = settings.max_entries_per_day
    if cap is not None and manual.entries_today >= cap:
        return f"already {manual.entries_today} manual entries today (limit {cap})"
    limit = settings.max_daily_loss
    if limit is not None and -manual.pnl_today >= limit - EPS:
        return f"today's manual loss {-manual.pnl_today:,.2f} reached the limit {limit:,.2f}"
    return None


@register_rule
class ManualDiscipline(RiskRule):
    name = "manual_discipline"
    order = 4

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: ManualDisciplineSettings | None = settings_of(ctx.policy, self.name)
        manual = manual_of(ctx)
        if settings is None or not settings.active or manual is None:
            return list(orders), []
        why = refusal(settings, manual)
        if why is None:
            return list(orders), []
        positions = dict(ctx.portfolio.positions)
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            if not _opens(order, positions):
                kept.append(order)
                continue
            adjustments.append(
                RiskAdjustment(
                    ticker=order.ticker,
                    side=order.side,
                    rule=self.name,
                    original_quantity=order.quantity,
                    adjusted_quantity=0.0,
                    reason=why,
                )
            )
        return kept, adjustments
