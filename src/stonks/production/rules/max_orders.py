"""At most N orders per live run, with a runaway halt (roadmap 19.6).

A bug that emits 500 orders must not reach the broker:

- opening orders beyond ``max_opening_orders`` are dropped, lowest signal
  score first (``decision_context["score"]``, unscored ones last, ties in
  the order given);
- a run that tries to close more than ``max_closing_orders`` positions is
  a **runaway**. Its closing orders are never dropped (P28), but every
  opening order is, and each close carries a ``runaway`` adjustment. The
  ``live_runaway`` hook then opens a ``runaway`` halt (buys) for the
  portfolio and alerts. Holding the closes for a person (approval
  tickets) is roadmap 19.8.

Runs last, after every other rule has shaped the orders. Acts only on live
books. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, is_opening_order, settings_of

#: The adjustment tag a runaway run carries on each of its closes.
RUNAWAY = "runaway"


class MaxOrdersPerRunSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_opening_orders: int | None = Field(default=None, ge=0)
    max_closing_orders: int | None = Field(default=None, ge=0)

    @property
    def active(self) -> bool:
        return self.max_opening_orders is not None or self.max_closing_orders is not None


def _score(order: Order) -> float | None:
    ctx = order.decision_context or {}
    value = ctx.get("score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@register_rule
class MaxOrdersPerRun(RiskRule):
    name = "max_orders_per_run"
    order = 80

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: MaxOrdersPerRunSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active or ctx.live is None:
            return list(orders), []
        opening = [o for o in orders if is_opening_order(o, ctx)]
        closing = [o for o in orders if not is_opening_order(o, ctx)]
        adjustments: list[RiskAdjustment] = []
        ceiling = settings.max_closing_orders
        if ceiling is not None and len(closing) > ceiling:
            reason = f"runaway: {len(closing)} closing orders over the ceiling {ceiling}"
            adjustments += [adjustment(o, RUNAWAY, o.quantity, reason) for o in closing]
            adjustments += [
                adjustment(o, self.name, 0.0, f"{reason}; no opening order is sent")
                for o in opening
            ]
            return list(closing), adjustments
        limit = settings.max_opening_orders
        if limit is None or len(opening) <= limit:
            return list(orders), []
        ranked = sorted(
            range(len(opening)),
            key=lambda i: (_score(opening[i]) is None, -(_score(opening[i]) or 0.0), i),
        )
        allowed = {id(opening[i]) for i in ranked[:limit]}
        kept: list[Order] = []
        for order in orders:
            if not is_opening_order(order, ctx) or id(order) in allowed:
                kept.append(order)
            else:
                adjustments.append(
                    adjustment(
                        order,
                        self.name,
                        0.0,
                        f"{len(opening)} opening orders over the per-run limit {limit}",
                    )
                )
        return kept, adjustments
