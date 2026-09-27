"""The order rate cap of an intraday book (roadmap 21.3.2).

A bug in an event loop can send an order on every tick. This rule caps
the orders a book sends in any 60 seconds (``max_orders_per_minute``) and
in the day (``max_orders_per_day``), counting what ``IntradayContext.sent_at``
says already went out before the event.

Closing orders are never dropped (P28): they use the room first, even past
the cap. Opening orders fill what room is left, highest signal score first
(``decision_context["score"]``, unscored ones last, ties in the order
given), and the rest are dropped with an ``intraday_order_rate`` tag.
:func:`stonks.production.intraday_halts.trip_intraday_runaway` turns those
tags into a ``runaway`` halt on new buys, as ``max_orders_per_run`` does
for a daily run.

Runs just before ``max_orders_per_run``, after the other rules shaped the
orders. Acts only on an intraday book. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, is_opening_order, settings_of
from stonks.production.rules._intraday import intraday_of

__all__ = ["IntradayOrderRate", "IntradayOrderRateSettings"]

WINDOW = timedelta(seconds=60)


class IntradayOrderRateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_orders_per_minute: int | None = Field(default=None, ge=1)
    max_orders_per_day: int | None = Field(default=None, ge=1)

    @property
    def active(self) -> bool:
        return self.max_orders_per_minute is not None or self.max_orders_per_day is not None


def _score(order: Order) -> float | None:
    value = (order.decision_context or {}).get("score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@register_rule
class IntradayOrderRate(RiskRule):
    name = "intraday_order_rate"
    order = 79
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: IntradayOrderRateSettings | None = settings_of(ctx.policy, self.name)
        intraday = intraday_of(ctx)
        if settings is None or not settings.active or intraday is None:
            return list(orders), []
        now = intraday.now
        sent = [t for t in intraday.sent_at if t <= now]
        rooms: list[tuple[int, str]] = []
        if settings.max_orders_per_minute is not None:
            in_minute = sum(1 for t in sent if t > now - WINDOW)
            rooms.append(
                (
                    settings.max_orders_per_minute - in_minute,
                    f"{settings.max_orders_per_minute} orders per minute",
                )
            )
        if settings.max_orders_per_day is not None:
            today = sum(1 for t in sent if t.date() == now.date())
            rooms.append(
                (settings.max_orders_per_day - today, f"{settings.max_orders_per_day} orders a day")
            )
        room, limit = min(rooms)
        opening = [o for o in orders if is_opening_order(o, ctx)]
        closes = len(orders) - len(opening)
        allowed_n = max(room - closes, 0)
        if len(opening) <= allowed_n:
            return list(orders), []
        ranked = sorted(
            range(len(opening)),
            key=lambda i: (_score(opening[i]) is None, -(_score(opening[i]) or 0.0), i),
        )
        allowed = {id(opening[i]) for i in ranked[:allowed_n]}
        reason = f"{len(opening)} opening orders over the cap of {limit} (room {max(room, 0)})"
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            if not is_opening_order(order, ctx) or id(order) in allowed:
                kept.append(order)
            else:
                adjustments.append(adjustment(order, self.name, 0.0, reason))
        return kept, adjustments
