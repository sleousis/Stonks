"""Today's portfolio caps (roadmap 2.3), one ``OrderRule`` each, in the order
they have always run: sells clipped to the held quantity; buys dropped
without a price, then clipped by ``max_open_positions``,
``max_weight_per_ticker``, ``max_weight_per_asset_class`` and the cash
buffer (net of the fill-cost estimate), then dropped below
``min_order_notional``. Weights are fractions of the value before the
tick's orders. The adjustment tags (``RiskAdjustment.rule``) are unchanged.
"""

from __future__ import annotations

from stonks.core.types import Order
from stonks.production.rules import (
    EPS,
    OrderRule,
    Recorder,
    RiskBook,
    RiskContext,
    clip,
    register_rule,
)


@register_rule
class SellWithinPosition(OrderRule):
    """Sells are never blocked, only clipped so they cannot open a short,
    unless the book allows shorts and the sell is an opening one (a short
    sale, ``position_effect="open"``): the short rules limit those."""

    name = "sell_within_position"
    order = 10

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        if order.side != "sell":
            return qty
        if ctx.allow_short and order.position_effect == "open":
            return qty
        held = max(book.positions.get(order.ticker, 0.0), 0.0)
        if qty > held:
            record(
                order,
                "sell_exceeds_position",
                held,
                f"sell of {qty} exceeds held {held}; clipped so it cannot open a short",
            )
            qty = held
        return None if qty <= EPS else qty


@register_rule
class RequirePrice(OrderRule):
    name = "require_price"
    order = 20

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        if order.side == "buy" and _price(order, ctx) is None:
            record(order, "no_price", 0.0, "no current price; cannot size the order")
            return None
        return qty


@register_rule
class MaxOpenPositions(OrderRule):
    """Only a buy that opens a new position counts."""

    name = "max_open_positions"
    order = 30

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        limit = ctx.policy.max_open_positions
        if order.side != "buy" or limit is None:
            return qty
        if book.positions.get(order.ticker, 0.0) > EPS:
            return qty
        open_count = sum(1 for q in book.positions.values() if q > EPS)
        if open_count >= limit:
            record(
                order, "max_open_positions", 0.0, f"{open_count} open positions >= limit {limit}"
            )
            return None
        return qty


@register_rule
class MaxWeightPerTicker(OrderRule):
    name = "max_weight_per_ticker"
    order = 40

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        price = _price(order, ctx)
        if price is None:
            return qty
        held = book.positions.get(order.ticker, 0.0)
        room = ctx.policy.max_weight_per_ticker * book.equity - max(held, 0.0) * price
        return clip(order, qty, room / price, "max_weight_per_ticker", record)


@register_rule
class MaxWeightPerAssetClass(OrderRule):
    """With any class cap set, a buy whose class is unknown, or whose class
    has an unpriced holding (exposure can't be bounded), is dropped."""

    name = "max_weight_per_asset_class"
    order = 50

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        caps = ctx.policy.max_weight_per_asset_class
        price = _price(order, ctx)
        if price is None or not caps:
            return qty
        classes, prices = ctx.asset_classes, ctx.prices
        cls = classes.get(order.ticker)
        if cls is None:
            record(
                order,
                "unknown_asset_class",
                0.0,
                "asset class unknown while asset-class caps are configured",
            )
            return None
        cap = caps.get(cls)  # type: ignore[call-overload]
        if cap is None:
            return qty
        held = [(t, q) for t, q in book.positions.items() if q > 0 and classes.get(t) == cls]
        unpriced = sorted(t for t, _ in held if not prices.get(t))
        if unpriced:
            record(
                order,
                "unpriced_holding",
                0.0,
                f"{cls} exposure unknown: no price for held {', '.join(unpriced)}",
            )
            return None
        class_value = sum(q * prices.get(t, 0.0) for t, q in held)
        room = cap * book.equity - class_value
        return clip(order, qty, room / price, "max_weight_per_asset_class", record)


@register_rule
class CashBuffer(OrderRule):
    """Keeps ``cash_buffer_fraction`` of the value in cash after the buy's
    expected outlay (the configured cost model, else legacy slippage/fee)."""

    name = "cash_buffer"
    order = 60

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        if _price(order, ctx) is None:
            return qty
        budget = book.cash - ctx.policy.cash_buffer_fraction * book.equity
        max_qty = ctx.affordable_quantity(order.ticker, qty, budget)
        return clip(order, qty, max_qty, "cash_buffer", record)


@register_rule
class MinOrderNotional(OrderRule):
    """Applied after all clipping."""

    name = "min_order_notional"
    order = 70

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        price = _price(order, ctx)
        if price is None:
            return qty
        notional = qty * price
        minimum = ctx.policy.min_order_notional
        if notional < minimum:
            record(order, "min_order_notional", 0.0, f"notional {notional:.2f} < min {minimum}")
            return None
        return qty


def _price(order: Order, ctx: RiskContext) -> float | None:
    """A buy's positive price; ``None`` for sells and unpriced buys (which
    ``RequirePrice`` drops before any sizing rule sees them)."""
    if order.side != "buy":
        return None
    price = ctx.prices.get(order.ticker)
    return price if price and price > 0 else None
