"""Sector cap (BL-27; Grinold & Kahn): the gross value held in one sector
(``instruments.sector``) may not exceed ``max_weight_per_sector`` of the
value before the tick's orders. Counts holdings after the sells and the
buys already allowed this tick.

Buys of tickers without a known sector (most non-equities) are not capped
by this rule; ``max_weight_per_asset_class`` covers those. A buy in a
sector with an unpriced holding is dropped, since its exposure can't be
bounded (``unpriced_holding``, as the asset-class cap does). A buy that
covers a short may always bring the position back to flat. Off by default.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import (
    EPS,
    OrderRule,
    Recorder,
    RiskBook,
    RiskContext,
    register_rule,
)
from stonks.production.rules._common import clip_with_reason, settings_of


class SectorCapSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_weight_per_sector: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def active(self) -> bool:
        return self.max_weight_per_sector is not None


@register_rule
class SectorCap(OrderRule):
    name = "sector_cap"
    order = 54
    #: Sectors come from ``build_risk_context``.
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        settings: SectorCapSettings | None = settings_of(ctx.policy, self.name)
        if order.side != "buy" or settings is None or settings.max_weight_per_sector is None:
            return qty
        price = ctx.prices.get(order.ticker)
        sector = ctx.sectors.get(order.ticker)
        if not price or price <= 0 or sector is None:
            return qty
        others = [
            (t, q)
            for t, q in book.positions.items()
            if t != order.ticker and abs(q) > EPS and ctx.sectors.get(t) == sector
        ]
        unpriced = sorted(t for t, _ in others if not ctx.prices.get(t))
        if unpriced:
            record(
                order,
                "unpriced_holding",
                0.0,
                f"sector {sector} exposure unknown: no price for held {', '.join(unpriced)}",
            )
            return None
        other_value = sum(abs(q) * ctx.prices[t] for t, q in others)
        cap_value = settings.max_weight_per_sector * max(book.equity, 0.0)
        limit = max((cap_value - other_value) / price, 0.0)
        held = book.positions.get(order.ticker, 0.0)
        detail = (
            f"sector {sector}: {settings.max_weight_per_sector:.2%} cap, "
            f"{other_value:.2f} held in other names"
        )
        return clip_with_reason(order, qty, limit - held, "max_weight_per_sector", detail, record)
