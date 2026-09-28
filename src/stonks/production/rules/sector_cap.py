"""Sector cap (BL-27; Grinold & Kahn): the gross value held in one sector
(``instruments.sector``) may not exceed ``max_weight_per_sector`` of the
value before the tick's orders. Counts holdings after the sells and the
buys already allowed this tick.

Buys of tickers without a known sector (most non-equities) are not capped
by this rule; ``max_weight_per_asset_class`` covers those. A buy in a
sector with an unpriced holding is dropped, since its exposure can't be
bounded (``unpriced_holding``, as the asset-class cap does). A buy that
covers a short may always bring the position back to flat. Off by default.

With ``look_through`` (roadmap 23.14, off by default) a held fund counts
as the sectors it owns, by its latest holdings list known on the day
(``RiskContext.fund_sectors``): a stock buy sees the tech inside SPY, and
a fund buy is capped by each sector it adds to. The plain cap still
applies, so turning it on only ever tightens.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    #: Count what held funds own, by their holdings lists (roadmap 23.14).
    look_through: bool = False

    @property
    def active(self) -> bool:
        return self.max_weight_per_sector is not None


def wants_look_through(policy: Any) -> bool:
    """``policy`` turns the cap and its look-through on, so a caller with a
    lake should fill ``RiskContext.fund_sectors``."""
    settings = settings_of(policy, "sector_cap")
    return settings is not None and settings.active and settings.look_through


def _weights(ticker: str, ctx: RiskContext, look: bool) -> Mapping[str, float]:
    """Each sector's share of one unit of ``ticker``'s value."""
    if look and ticker in ctx.fund_sectors:
        return ctx.fund_sectors[ticker]
    sector = ctx.sectors.get(ticker)
    return {sector: 1.0} if sector is not None else {}


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
        if not price or price <= 0:
            return qty
        cap = settings.max_weight_per_sector
        cap_value = cap * max(book.equity, 0.0)
        held = book.positions.get(order.ticker, 0.0)
        plain = ctx.sectors.get(order.ticker)
        limits: list[tuple[float, str]] = []
        if plain is not None:
            found = self._room(order, book, ctx, {plain: 1.0}, False, price, cap_value, record)
            if found is None:
                return None
            limits.append(found)
        if settings.look_through and (order.ticker in ctx.fund_sectors or plain is not None):
            found = self._room(
                order, book, ctx, _weights(order.ticker, ctx, True), True, price, cap_value, record
            )
            if found is None:
                return None
            limits.append(found)
        if not limits:
            return qty
        limit, detail = min(limits, key=lambda item: item[0])
        return clip_with_reason(
            order, qty, limit - held, "max_weight_per_sector", f"{cap:.2%} cap, {detail}", record
        )

    @staticmethod
    def _room(
        order: Order,
        book: RiskBook,
        ctx: RiskContext,
        weights: Mapping[str, float],
        look: bool,
        price: float,
        cap_value: float,
        record: Recorder,
    ) -> tuple[float, str] | None:
        """The largest position in ``order.ticker`` that keeps every sector
        it adds to under the cap, with the reason, or ``None`` (recorded)
        when a holding in one of those sectors has no price."""
        best: tuple[float, str] | None = None
        for sector, weight in weights.items():
            if weight <= EPS:
                continue
            others = [
                (t, q, share)
                for t, q in book.positions.items()
                if t != order.ticker and abs(q) > EPS
                for share in (_weights(t, ctx, look).get(sector, 0.0),)
                if share > EPS
            ]
            unpriced = sorted(t for t, _, _ in others if not ctx.prices.get(t))
            if unpriced:
                record(
                    order,
                    "unpriced_holding",
                    0.0,
                    f"sector {sector} exposure unknown: no price for held {', '.join(unpriced)}",
                )
                return None
            other_value = sum(abs(q) * ctx.prices[t] * share for t, q, share in others)
            limit = max((cap_value - other_value) / (price * weight), 0.0)
            where = " through funds" if look else ""
            detail = f"sector {sector}: {other_value:.2f} held{where} in other names"
            if best is None or limit < best[0]:
                best = (limit, detail)
        return best if best is not None else (float("inf"), "")
