"""Short position caps (roadmap 16.2).

- ``max_short_weight``: no single short may be worth more than this share
  of the book's value (before the tick's orders) once the orders fill.
  Each short sale is clipped to what is left under its name's cap.
- ``max_short_total``: the shorts together may not exceed this share;
  over it, every short sale is scaled by one common factor.

Only short sales (opening sells) are cut. Covers and sells of longs pass.
Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, largest_scale, settings_of
from stonks.production.rules._shorts import (
    exposure_after_scale,
    is_opening,
    price,
    scale_orders,
    signed,
    split_opening,
)


class ShortCapsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_short_weight: float | None = Field(default=None, ge=0.0)
    max_short_total: float | None = Field(default=None, ge=0.0)

    @property
    def active(self) -> bool:
        return self.max_short_weight is not None or self.max_short_total is not None


def _is_short_sale(order: Order, positions: dict[str, float]) -> bool:
    return order.side == "sell" and is_opening(order, positions)


@register_rule
class ShortCaps(RiskRule):
    name = "short_caps"
    order = 8

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: ShortCapsSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active:
            return list(orders), []
        equity = ctx.portfolio.total_value(dict(ctx.prices))
        start = dict(ctx.portfolio.positions)
        current: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        running = dict(start)
        for order in orders:
            if not _is_short_sale(order, start) or settings.max_short_weight is None:
                current.append(order)
                running[order.ticker] = running.get(order.ticker, 0.0) + signed(order)
                continue
            p = price(ctx, order.ticker)
            held = running.get(order.ticker, 0.0)
            room = 0.0
            if p is not None and equity > 0:
                room = max(settings.max_short_weight * equity / p + min(held, 0.0), 0.0)
            qty = min(order.quantity, room)
            if qty < order.quantity:
                adjustments.append(
                    adjustment(
                        order,
                        self.name,
                        qty,
                        f"short weight cap {settings.max_short_weight:.2%} leaves {room:.4f}",
                    )
                )
            if qty > EPS:
                kept = order if qty == order.quantity else replace(order, quantity=qty)
                current.append(kept)
                running[order.ticker] = held - qty
        cap = settings.max_short_total
        if cap is None:
            return current, adjustments
        base, shorts = split_opening(current, start, lambda o: o.side == "sell")
        if not shorts:
            return current, adjustments
        scale = 0.0
        if equity > 0:
            f = exposure_after_scale(base, shorts, ctx, lambda q: max(-q, 0.0))
            scale = largest_scale(f, cap)
        current, adj = scale_orders(
            current, shorts, scale, self.name, f"total short weight over {cap:.2%}"
        )
        return current, adjustments + adj
