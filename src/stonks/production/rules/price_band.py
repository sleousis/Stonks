"""Fat-finger price bands for live books (roadmap 19.6).

Every order of a live book leaves with a collared limit price:

- a buy may pay at most the reference plus ``band_pct``, and at most the
  ask plus ``nbbo_band_pct`` when a live quote exists;
- a sell may take at least the reference minus ``band_pct``, and at least
  the bid minus ``nbbo_band_pct`` when a live quote exists;
- with only a delayed quote or the lake close, the band tightens to
  ``delayed_band_pct`` (never looser than ``band_pct``).

A market order becomes a limit at the collar. An existing limit is only
ever tightened. Stop orders (protective stops) pass untouched.

An opening order whose reference moved more than ``max_gap_pct`` from its
decision price is dropped. An opening order with no reference at all is
dropped. A closing order is never dropped (P28): with no reference it goes
out unbanded and the adjustment says so. Only prices change, never a
quantity. Acts only on live books. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.live.quotes import Reference, reference_price
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, is_opening_order, settings_of


class PriceBandSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Limit within this fraction of the reference; ``None`` is off.
    band_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    #: And within this fraction outside the bid or ask of a live quote.
    nbbo_band_pct: float = Field(default=0.01, gt=0.0, lt=1.0)
    #: The band with only a delayed quote or the lake close.
    delayed_band_pct: float = Field(default=0.01, gt=0.0, lt=1.0)
    #: Drop an opening order when the price moved more than this since the
    #: decision; ``None`` never drops.
    max_gap_pct: float | None = Field(default=None, gt=0.0, lt=1.0)

    @property
    def active(self) -> bool:
        return self.band_pct is not None


def collar(order: Order, ref: Reference, settings: PriceBandSettings) -> float:
    """The worst price ``order`` may trade at under the band."""
    band = settings.band_pct or 0.0
    if not ref.is_live:
        band = min(band, settings.delayed_band_pct)
    if order.side == "buy":
        limit = ref.price * (1 + band)
        if ref.ask is not None:
            limit = min(limit, ref.ask * (1 + settings.nbbo_band_pct))
        return limit
    limit = ref.price * (1 - band)
    if ref.bid is not None:
        limit = max(limit, ref.bid * (1 - settings.nbbo_band_pct))
    return limit


@register_rule
class PriceBand(RiskRule):
    name = "price_band"
    order = 9

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: PriceBandSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active or ctx.live is None:
            return list(orders), []
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            if order.order_type == "stop":
                kept.append(order)
                continue
            opening = is_opening_order(order, ctx)
            ref = reference_price(ctx, order.ticker)
            if ref is None:
                if opening:
                    adjustments.append(
                        adjustment(order, self.name, 0.0, "no reference price; cannot band it")
                    )
                else:
                    adjustments.append(
                        adjustment(
                            order,
                            self.name,
                            order.quantity,
                            "no reference price; closing order sent unbanded",
                        )
                    )
                    kept.append(order)
                continue
            gap = _gap(order, ref)
            limit = settings.max_gap_pct
            if opening and limit is not None and gap is not None and gap > limit:
                adjustments.append(
                    adjustment(
                        order,
                        self.name,
                        0.0,
                        f"price moved {gap:.2%} since the decision (limit {limit:.2%})",
                    )
                )
                continue
            banded = _banded(order, collar(order, ref, settings))
            if banded.limit_price != order.limit_price or banded.order_type != order.order_type:
                adjustments.append(
                    adjustment(
                        order,
                        self.name,
                        order.quantity,
                        f"limit set to {banded.limit_price:.4f} "
                        f"(reference {ref.price:.4f}, {ref.source})",
                    )
                )
            kept.append(banded)
        return kept, adjustments


def _gap(order: Order, ref: Reference) -> float | None:
    decided = order.decision_price
    if decided is None or decided <= 0:
        return None
    return abs(ref.price / decided - 1.0)


def _banded(order: Order, limit: float) -> Order:
    if order.order_type == "market":
        return replace(order, order_type="limit", limit_price=limit)
    current = order.limit_price
    if current is None:
        return replace(order, limit_price=limit)
    tighter = min(current, limit) if order.side == "buy" else max(current, limit)
    return order if tighter == current else replace(order, limit_price=tighter)
