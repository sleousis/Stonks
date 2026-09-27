"""Gross and net exposure limits (roadmap 16.2).

Weights are market value over the book's value before the tick's orders,
signed (a short is negative):

- ``gross_exposure``: ``sum |w| <= max_gross`` once every order has
  filled. Over the limit, every opening order (buys that open or grow a
  long, short sales) is scaled by one common factor, the largest that fits.
- ``net_exposure``: ``min_net <= sum w <= max_net``. Above ``max_net`` the
  opening buys are scaled down; below ``min_net`` the short sales are.

Closing orders (sells of longs, covers) are never touched, so neither rule
can raise gross exposure or block a position-reducing order. A book that
already sits outside a limit before any order only loses its opening
orders. Both are off by default (``None``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import largest_scale, settings_of
from stonks.production.rules._shorts import exposure_after_scale, scale_orders, split_opening


class GrossExposureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``sum |w|`` cap (1.0 is fully invested; 2.0 allows 130/30 or 100/100).
    max_gross: float | None = Field(default=None, ge=0.0)

    @property
    def active(self) -> bool:
        return self.max_gross is not None


class NetExposureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_net: float | None = None
    max_net: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> NetExposureSettings:
        if self.min_net is not None and self.max_net is not None and self.min_net > self.max_net:
            raise ValueError(f"min_net {self.min_net} exceeds max_net {self.max_net}")
        return self

    @property
    def active(self) -> bool:
        return self.min_net is not None or self.max_net is not None


def _has_equity(ctx: RiskContext) -> bool:
    return ctx.portfolio.total_value(dict(ctx.prices)) > 0


@register_rule
class GrossExposure(RiskRule):
    name = "gross_exposure"
    order = 6

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: GrossExposureSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or settings.max_gross is None:
            return list(orders), []
        base, opening = split_opening(orders, ctx.portfolio.positions, lambda o: True)
        if not opening:
            return list(orders), []
        cap = settings.max_gross
        scale = 0.0
        if _has_equity(ctx):
            scale = largest_scale(exposure_after_scale(base, opening, ctx, abs), cap)
        return scale_orders(
            orders, opening, scale, self.name, f"gross exposure over {cap:.2f}; opens x{scale:.4f}"
        )


@register_rule
class NetExposure(RiskRule):
    name = "net_exposure"
    order = 7

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: NetExposureSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active:
            return list(orders), []
        current = list(orders)
        adjustments: list[RiskAdjustment] = []
        positions = ctx.portfolio.positions
        if settings.max_net is not None:
            base, buys = split_opening(current, positions, lambda o: o.side == "buy")
            if buys:
                f = exposure_after_scale(base, buys, ctx, lambda q: q)
                cap = settings.max_net
                scale = largest_scale(f, cap) if _has_equity(ctx) else 0.0
                current, adj = scale_orders(
                    current, buys, scale, self.name, f"net exposure over {cap:.2f}"
                )
                adjustments += adj
        if settings.min_net is not None:
            base, shorts = split_opening(current, positions, lambda o: o.side == "sell")
            if shorts:
                f = exposure_after_scale(base, shorts, ctx, lambda q: -q)
                floor = settings.min_net
                scale = largest_scale(f, -floor) if _has_equity(ctx) else 0.0
                current, adj = scale_orders(
                    current, shorts, scale, self.name, f"net exposure under {floor:.2f}"
                )
                adjustments += adj
        return current, adjustments
