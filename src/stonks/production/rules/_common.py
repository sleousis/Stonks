"""Helpers shared by the W3.1 risk rules (BL-27): settings lookup, causal
history, volatility estimates and buy scaling.

Every estimate reads only bars dated on or before ``ctx.as_of`` (when set),
so a context carrying later bars cannot leak the future into a decision.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.types import Order
from stonks.features.indicators import atr
from stonks.features.volatility import ewma_vol
from stonks.logging import get_logger
from stonks.production.rules import EPS, Recorder, RiskAdjustment, RiskContext

_log = get_logger("stonks.production.rules")

#: One-sided 99% normal quantile, for one-day VaR.
Z99 = 2.33
#: Bars in the ATR and liquidity windows.
ATR_BARS = 20
LIQUIDITY_BARS = 20
#: EWMA span (bars) of the single-name daily sigma.
SIGMA_SPAN = 35


def settings_of(policy: Any, key: str) -> Any:
    """The ``key`` settings of ``policy.rules`` (the ``RuleSettings`` the
    integration step adds to ``RiskPolicy``), or ``None`` without them."""
    rules = getattr(policy, "rules", None)
    return getattr(rules, key, None) if rules is not None else None


def history(ctx: RiskContext, ticker: str) -> pd.DataFrame | None:
    """``ticker``'s bars up to ``ctx.as_of``, oldest first; ``None`` if absent."""
    frame = ctx.history.get(ticker)
    if frame is None or frame.empty:
        return None
    if ctx.as_of is not None:
        frame = frame[frame.index <= pd.Timestamp(ctx.as_of)]
    return frame if not frame.empty else None


def marks(ctx: RiskContext) -> dict[str, float] | None:
    """The context's prices, a held ticker with none carried at its last
    close in the history (BE-45). ``None`` when a holding still has no
    mark: the book's value is unknown, so value-based rules skip rather
    than read the holding as worth 0."""
    out = {t: float(p) for t, p in ctx.prices.items() if p is not None and math.isfinite(p)}
    for ticker in ctx.portfolio.unmarked(out):
        if abs(ctx.portfolio.positions.get(ticker, 0.0)) <= EPS:
            continue
        frame = history(ctx, ticker)
        last = float(frame["close"].iloc[-1]) if frame is not None else math.nan
        if not (math.isfinite(last) and last > 0):
            _log.warning("risk.unmarked_holding", ticker=ticker)
            return None
        out[ticker] = last
    return out


def book_value(ctx: RiskContext) -> float | None:
    """The book's value at :func:`marks`, or ``None`` when unknown."""
    prices = marks(ctx)
    return None if prices is None else ctx.portfolio.total_value(prices)


def account_value(ctx: RiskContext) -> float | None:
    """The whole account's value: the book's (:func:`book_value`) plus the
    positions it does not own (``ctx.outside_positions``), as the equity
    curve records it. ``None`` when any of them has no mark."""
    value = book_value(ctx)
    if value is None:
        return None
    for ticker, qty in ctx.outside_positions.items():
        if abs(qty) <= EPS:
            continue
        price = ctx.prices.get(ticker)
        if price is None or not math.isfinite(price) or price <= 0:
            frame = history(ctx, ticker)
            price = float(frame["close"].iloc[-1]) if frame is not None else math.nan
        if not (math.isfinite(price) and price > 0):
            _log.warning("risk.unmarked_outside_holding", ticker=ticker)
            return None
        value += qty * price
    return value


def last_atr(ctx: RiskContext, ticker: str, bars: int = ATR_BARS) -> float | None:
    """Wilder ATR over ``bars`` at the last bar, or ``None`` without enough."""
    frame = history(ctx, ticker)
    if frame is None or len(frame) <= bars:
        return None
    value = atr(frame["high"], frame["low"], frame["close"], bars).iloc[-1]
    return _positive(value)


def daily_sigma(ctx: RiskContext, ticker: str, span: int = SIGMA_SPAN) -> float | None:
    """Zero-mean EWMA sigma of daily log returns at the last bar."""
    frame = history(ctx, ticker)
    if frame is None or len(frame) <= span:
        return None
    returns = np.log(frame["close"]).diff()
    return _positive(ewma_vol(returns, span=span).iloc[-1])


def _positive(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def clip_with_reason(
    order: Order, qty: float, max_qty: float, rule: str, detail: str, record: Recorder
) -> float | None:
    """Like ``rules.clip`` with the limit's derivation in the reason."""
    if qty <= max_qty:
        return qty
    new_qty = max(max_qty, 0.0)
    record(order, rule, new_qty, f"quantity {qty} exceeds {rule} room {new_qty} ({detail})")
    return None if new_qty <= EPS else new_qty


def adjustment(
    order: Order, rule: str, new_qty: float, reason: str, original: float | None = None
) -> RiskAdjustment:
    """A logged ``RiskAdjustment``; ``original`` overrides the order's
    quantity (0 for an order a rule adds)."""
    adj = RiskAdjustment(
        ticker=order.ticker,
        side=order.side,
        rule=rule,
        original_quantity=order.quantity if original is None else original,
        adjusted_quantity=max(new_qty, 0.0),
        reason=reason,
    )
    _log.info("risk.adjusted", client_id=order.client_id, **adj.as_dict())
    return adj


def positions_after_sells(orders: Sequence[Order], ctx: RiskContext) -> dict[str, float]:
    """Held quantities once the proposed sells (clipped to what is held,
    as ``sell_within_position`` will) have gone through."""
    positions = dict(ctx.portfolio.positions)
    for order in orders:
        if order.side == "sell":
            held = positions.get(order.ticker, 0.0)
            positions[order.ticker] = held - min(order.quantity, max(held, 0.0))
    return positions


def is_opening(order: Order, positions: Mapping[str, float]) -> bool:
    """The order opens or grows a position, long or short. With a position
    effect (orders split at zero) that decides; otherwise it is read from
    the held quantity: a buy opens unless it covers a short, a sell opens
    only from a flat or short position."""
    if order.position_effect is not None:
        return order.position_effect == "open"
    held = positions.get(order.ticker, 0.0)
    return held >= 0 if order.side == "buy" else held <= 0


def is_opening_order(order: Order, ctx: RiskContext) -> bool:
    """:func:`is_opening` against the context's book. Closes (sells of a
    long, covers of a short) reduce risk and are left alone by the halt and
    scaling rules (BE-05)."""
    return is_opening(order, ctx.portfolio.positions)


def scale_opens(
    orders: Sequence[Order],
    ctx: RiskContext,
    scale: float,
    rule: str,
    reason: str,
    only: set[str] | None = None,
) -> tuple[list[Order], list[RiskAdjustment]]:
    """Multiply opening orders, buys and short sales (of ``only`` tickers,
    when given), by ``scale`` in ``[0, 1]``; orders that shrink to nothing
    are dropped. Closes pass untouched."""
    scale = min(max(scale, 0.0), 1.0)
    if scale >= 1.0:
        return list(orders), []
    kept: list[Order] = []
    adjustments: list[RiskAdjustment] = []
    for order in orders:
        if not is_opening_order(order, ctx) or (only is not None and order.ticker not in only):
            kept.append(order)
            continue
        qty = order.quantity * scale
        adjustments.append(adjustment(order, rule, qty, reason))
        if qty > EPS:
            kept.append(replace(order, quantity=qty))
    return kept, adjustments


def largest_scale(f: Any, cap: float, iterations: int = 60) -> float:
    """The largest ``s`` in ``[0, 1]`` with ``f(s) <= cap`` for a convex
    ``f`` (a norm of positions affine in ``s``); 0 when even ``f(0)`` is over."""
    if f(1.0) <= cap:
        return 1.0
    if f(0.0) > cap:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if f(mid) <= cap:
            lo = mid
        else:
            hi = mid
    return lo
