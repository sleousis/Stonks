"""Notional caps for live books (roadmap 19.6).

Opening orders are clipped, in the order given, so that:

- no single order is worth more than ``max_order_notional``;
- the portfolio's opening orders today stay within ``max_day_notional``;
- every portfolio of the owner stays within ``max_user_day_notional``;
- everyone together stays within ``max_global_day_notional``.

What was already sent today comes from ``ctx.live`` (read from ``orders``).
An order is valued at its limit price when it has one, else at the
reference price (``production.live.quotes``). An opening order with no
price at all is dropped. Closing orders are never touched (P28). Acts only
on live books. Every cap is off (``None``) by default. Portfolio and owner
overrides can only tighten them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.live.quotes import reference_price
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, is_opening_order, settings_of


class LiveNotionalCapsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Largest opening order, in the account's base currency.
    max_order_notional: float | None = Field(default=None, ge=0.0)
    #: Opening notional per portfolio per day.
    max_day_notional: float | None = Field(default=None, ge=0.0)
    #: Opening notional per owner (all their portfolios) per day.
    max_user_day_notional: float | None = Field(default=None, ge=0.0)
    #: Opening notional across every portfolio per day.
    max_global_day_notional: float | None = Field(default=None, ge=0.0)

    @property
    def active(self) -> bool:
        return any(
            v is not None
            for v in (
                self.max_order_notional,
                self.max_day_notional,
                self.max_user_day_notional,
                self.max_global_day_notional,
            )
        )


def order_price(order: Order, ctx: RiskContext) -> float | None:
    """The price an order is valued at: its limit, else the reference."""
    if order.limit_price is not None:
        return order.limit_price
    ref = reference_price(ctx, order.ticker)
    return ref.price if ref is not None else None


@register_rule
class LiveNotionalCaps(RiskRule):
    name = "live_notional_caps"
    order = 9

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: LiveNotionalCapsSettings | None = settings_of(ctx.policy, self.name)
        live = ctx.live
        if settings is None or not settings.active or live is None:
            return list(orders), []
        budgets = [
            live.room_left(settings.max_day_notional, live.sent_today),
            live.room_left(settings.max_user_day_notional, live.sent_today_user),
            live.room_left(settings.max_global_day_notional, live.sent_today_global),
        ]
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            if not is_opening_order(order, ctx):
                kept.append(order)
                continue
            px = order_price(order, ctx)
            if px is None:
                adjustments.append(
                    adjustment(order, self.name, 0.0, "no price to value the order against caps")
                )
                continue
            limits = [b for b in (settings.max_order_notional, *budgets) if b is not None]
            room = min(limits) if limits else None
            qty = order.quantity
            if room is not None and qty * px > room + 1e-9:
                qty = max(room / px, 0.0)
                adjustments.append(
                    adjustment(
                        order,
                        self.name,
                        qty,
                        f"notional {order.quantity * px:,.2f} over the live cap room {room:,.2f}",
                    )
                )
            if qty <= EPS:
                continue
            spent = qty * px
            budgets = [None if b is None else max(b - spent, 0.0) for b in budgets]
            kept.append(order if qty == order.quantity else replace(order, quantity=qty))
        return kept, adjustments
