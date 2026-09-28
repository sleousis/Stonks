"""Capital allocation cap for live books (roadmap 19.6).

The owner sets, by hand, how much Stonks may trade in each live portfolio
(``production.live.allocation``, with a fresh second factor and an audit
row). This rule keeps the book's gross exposure, once every order has
filled, at or under that amount, and under the account's net liquidation
value when the broker reports it. Over the cap, every opening order is
scaled by one common factor, the largest that fits. With no allocation,
nothing opens.

There are no automatic steps: a bad week raises an alert but never cuts
the allocation. Closing orders pass untouched (P28), and a book already
over the cap only loses its opening orders. Acts only on live books
(``ctx.live``). Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import largest_scale, settings_of
from stonks.production.rules._shorts import price, scale_orders, signed, split_opening


class CapitalRampSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False

    @property
    def active(self) -> bool:
        return self.enabled


def allocation_cap(ctx: RiskContext) -> float:
    """The most the live book may hold: the owner's allocation, capped at
    the account's net liquidation value when known. 0 without either."""
    live = ctx.live
    if live is None or live.allocation is None:
        return 0.0
    cap = max(live.allocation, 0.0)
    if live.account is not None:
        cap = min(cap, max(live.account.equity, 0.0))
    return cap


@register_rule
class CapitalRamp(RiskRule):
    name = "capital_ramp"
    order = 4

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: CapitalRampSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active or ctx.live is None:
            return list(orders), []
        base, opening = split_opening(orders, ctx.portfolio.positions, lambda o: True)
        if not opening:
            return list(orders), []
        if ctx.live.allocation is None:
            return scale_orders(
                orders, opening, 0.0, self.name, "no live allocation set; nothing may open"
            )
        cap = allocation_cap(ctx)

        def gross(s: float) -> float:
            pos = dict(base)
            for o in opening:
                pos[o.ticker] = pos.get(o.ticker, 0.0) + s * signed(o)
            return sum(abs(q) * (price(ctx, t) or 0.0) for t, q in pos.items())

        scale = largest_scale(gross, cap)
        return scale_orders(
            orders,
            opening,
            scale,
            self.name,
            f"gross exposure over the live allocation {cap:,.2f}; opens x{scale:.4f}",
        )
