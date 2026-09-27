"""Helpers shared by the short-selling risk rules (roadmap 16.2).

The rules see orders already split at zero (``apply_risk`` classifies them
when the book allows shorts), so each order either opens or closes. An
order without a position effect (a long-only book) is read from the
position: a buy opens unless it covers a short, a sell opens only from a
flat or short position.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime

from stonks.core.types import Order
from stonks.production.rules import EPS, RiskAdjustment, RiskContext
from stonks.production.rules._common import adjustment, is_opening


def decision_day(ctx: RiskContext) -> date:
    """The context's decision day, else today (UTC)."""
    return ctx.as_of or datetime.now(UTC).date()


def signed(order: Order) -> float:
    return order.quantity if order.side == "buy" else -order.quantity


def price(ctx: RiskContext, ticker: str) -> float | None:
    p = ctx.prices.get(ticker)
    return float(p) if p and p > 0 else None


def after(orders: Sequence[Order], positions: Mapping[str, float]) -> dict[str, float]:
    """Positions once every order has filled."""
    out = dict(positions)
    for order in orders:
        out[order.ticker] = out.get(order.ticker, 0.0) + signed(order)
    return out


def split_opening(
    orders: Sequence[Order], positions: Mapping[str, float], which: Callable[[Order], bool]
) -> tuple[dict[str, float], list[Order]]:
    """``(positions after every order but the selected opening ones, the
    selected opening orders)``."""
    chosen = [o for o in orders if is_opening(o, positions) and which(o)]
    ids = {id(o) for o in chosen}
    return after([o for o in orders if id(o) not in ids], positions), chosen


def exposure_after_scale(
    base: Mapping[str, float],
    opening: Sequence[Order],
    ctx: RiskContext,
    measure: Callable[[float], float],
) -> Callable[[float], float]:
    """``s -> sum_t measure(position_t) x price_t / equity`` with the
    ``opening`` orders scaled by ``s``."""
    equity = ctx.portfolio.total_value(dict(ctx.prices))

    def f(s: float) -> float:
        pos = dict(base)
        for o in opening:
            pos[o.ticker] = pos.get(o.ticker, 0.0) + s * signed(o)
        total = sum(measure(q) * (price(ctx, t) or 0.0) for t, q in pos.items())
        return total / equity

    return f


def scale_orders(
    orders: Sequence[Order],
    chosen: Sequence[Order],
    scale: float,
    rule: str,
    reason: str,
) -> tuple[list[Order], list[RiskAdjustment]]:
    """``orders`` with every ``chosen`` order multiplied by ``scale``;
    orders that shrink to nothing are dropped."""
    if scale >= 1.0:
        return list(orders), []
    ids = {id(o) for o in chosen}
    kept: list[Order] = []
    adjustments: list[RiskAdjustment] = []
    for order in orders:
        if id(order) not in ids:
            kept.append(order)
            continue
        qty = order.quantity * max(scale, 0.0)
        adjustments.append(adjustment(order, rule, qty, reason))
        if qty > EPS:
            kept.append(replace(order, quantity=qty))
    return kept, adjustments
