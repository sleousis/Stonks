"""Borrow check (roadmap 16.2): a short sale needs a locate.

Each short sale asks the borrow source for a quote on the decision day:
the book's own source (``ctx.borrow``) when the caller has one, else a
``FlatBorrow`` built from ``borrow`` in these settings. With neither there
is no quote, and no quote means no short (missing data never passes).

- status ``none`` (no locate): dropped;
- ``fee_rate_annual`` above ``max_borrow_fee``: dropped;
- ``available_shares``: the sale is clipped to it.

Covers and sells of longs pass. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.execution.borrow import BorrowQuote, BorrowSettings, BorrowSource
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, settings_of
from stonks.production.rules._shorts import decision_day, is_opening


class BorrowCheckSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    #: Short sales with a higher annual borrow fee are dropped.
    max_borrow_fee: float | None = Field(default=None, ge=0.0)
    #: The static source used when the caller brings none.
    borrow: BorrowSettings | None = None

    @property
    def active(self) -> bool:
        return self.enabled


def borrow_source(ctx: RiskContext, fallback: BorrowSettings | None) -> BorrowSource | None:
    """The book's borrow source, else one built from ``fallback``."""
    if isinstance(ctx.borrow, BorrowSource):
        return ctx.borrow
    return fallback.build() if fallback is not None else None


def quote_for(ctx: RiskContext, source: BorrowSource | None, ticker: str) -> BorrowQuote | None:
    if source is None:
        return None
    day = decision_day(ctx)
    return source.quote(ticker, day, ctx.asset_classes.get(ticker, "equity"))  # type: ignore[arg-type]


@register_rule
class BorrowCheck(RiskRule):
    name = "borrow_check"
    order = 9

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: BorrowCheckSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active:
            return list(orders), []
        source = borrow_source(ctx, settings.borrow)
        positions = ctx.portfolio.positions
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            if order.side != "sell" or not is_opening(order, positions):
                kept.append(order)
                continue
            quote = quote_for(ctx, source, order.ticker)
            reason = None
            qty = order.quantity
            if quote is None:
                reason = "no borrow quote; no short without a locate"
            elif not quote.shortable:
                reason = "not borrowable (no locate)"
            elif settings.max_borrow_fee is not None and (
                quote.fee_rate_annual > settings.max_borrow_fee
            ):
                reason = (
                    f"borrow fee {quote.fee_rate_annual:.2%} over {settings.max_borrow_fee:.2%}"
                )
            elif quote.available_shares is not None and qty > quote.available_shares:
                qty = max(quote.available_shares, 0.0)
                adjustments.append(
                    adjustment(order, self.name, qty, f"only {qty} shares to borrow")
                )
                if qty > EPS:
                    kept.append(replace(order, quantity=qty))
                continue
            if reason is None:
                kept.append(order)
            else:
                adjustments.append(adjustment(order, self.name, 0.0, reason))
        return kept, adjustments
