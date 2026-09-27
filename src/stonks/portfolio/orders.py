"""Target weights to orders, with Carver's no-trade buffer (BL-08, P23).

For each ticker held or targeted, with ``E`` the book's total value:

- a target of 0 (or a held ticker missing from the targets) is a full exit
  and always sells the whole position;
- otherwise there is no trade while ``|current - target| <=
  buffer_fraction * target``;
- outside that band the trade goes only to the nearest band edge, so small
  wiggles in the target don't churn the book;
- a trade smaller than ``min_trade_weight * E`` is skipped (exits excepted).

The result is safe for a long-only cash account: targets must be >= 0,
sells never exceed the position, and buys are scaled down pro rata so they
never spend more than cash plus this batch's sell proceeds. Tickers
without a positive price are skipped. Client ids come from
:func:`stonks.execution.orders.make_client_id`, so a rerun is idempotent.

Long/short books (``allow_short=True``, roadmap 16.1)
-----------------------------------------------------
Targets may be negative (a short weight). The band works on ``|target|``
(``buffer_fraction * |target|``), and a target whose sign differs from the
position always trades, to the band edge on the new side. Every order
carries its position effect and is split at zero
(:func:`stonks.execution.orders.classify`): long 10, target short 5 gives
sell-close 10 then sell-open (``short``) 5. Covers are never scaled; only
opening buys are scaled to cash plus this batch's sale proceeds (margin is
the risk layer's and the broker's job). With ``allow_short=False`` (the
default) the long-only rules above apply unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import date

from stonks.core.types import Order, OrderSide, Portfolio
from stonks.execution.orders import classify, make_client_id
from stonks.logging import get_logger

_log = get_logger("stonks.portfolio.orders")

DEFAULT_STRATEGY_ID = "portfolio"


def _price(prices: Mapping[str, float], ticker: str) -> float | None:
    p = prices.get(ticker)
    return float(p) if p is not None and math.isfinite(p) and p > 0 else None


def orders_from_targets(
    target_weights: Mapping[str, float],
    portfolio: Portfolio,
    prices: Mapping[str, float],
    buffer_fraction: float = 0.10,
    min_trade_weight: float = 0.005,
    *,
    as_of: date,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    allow_short: bool = False,
) -> list[Order]:
    """Orders that move ``portfolio`` towards ``target_weights`` (fractions
    of total value). Sells first, then buys, each in ticker order.
    ``allow_short`` accepts negative targets (see the module doc)."""
    if not 0.0 <= buffer_fraction <= 0.5:
        raise ValueError(f"buffer_fraction must be in [0, 0.5], got {buffer_fraction}")
    if min_trade_weight < 0:
        raise ValueError(f"min_trade_weight must be >= 0, got {min_trade_weight}")
    if allow_short:
        return _long_short_orders(
            target_weights,
            portfolio,
            prices,
            buffer_fraction,
            min_trade_weight,
            as_of=as_of,
            strategy_id=strategy_id,
        )
    shorts = {t: w for t, w in target_weights.items() if not (w >= 0 and math.isfinite(w))}
    if shorts:
        raise ValueError(f"targets must be finite and >= 0 (no short positions): {shorts}")

    equity = portfolio.total_value(prices)
    if not equity > 0:
        return []

    trades: dict[str, float] = {}  # ticker -> signed quantity
    unpriced: list[str] = []
    for ticker in sorted({*portfolio.positions, *target_weights}):
        price = _price(prices, ticker)
        if price is None:
            unpriced.append(ticker)
            continue
        held = portfolio.positions.get(ticker, 0.0)
        target = float(target_weights.get(ticker, 0.0))
        if target == 0.0:
            if held != 0.0:
                trades[ticker] = -held  # full exit, exact quantity
            continue
        current = held * price / equity
        band = buffer_fraction * target
        if abs(current - target) <= band:
            continue
        edge = target - band if current < target else target + band
        delta = edge - current
        if abs(delta) < min_trade_weight:
            continue
        quantity = delta * equity / price
        if quantity < 0:
            quantity = max(quantity, -held) if held > 0 else 0.0
        if quantity != 0.0:
            trades[ticker] = quantity
    if unpriced:
        _log.warning("portfolio.orders.unpriced.skipped", tickers=unpriced)

    proceeds = sum(-q * prices[t] for t, q in trades.items() if q < 0)
    spend = sum(q * prices[t] for t, q in trades.items() if q > 0)
    budget = max(portfolio.cash + proceeds, 0.0)
    scale = 1.0 if spend <= budget else budget / spend
    if scale < 1.0:
        _log.info("portfolio.orders.buys_scaled", scale=scale, spend=spend, budget=budget)

    def order(ticker: str, side: OrderSide, quantity: float) -> Order:
        return Order(
            client_id=make_client_id(
                as_of=as_of, strategy_id=strategy_id, ticker=ticker, side=side
            ),
            ticker=ticker,
            side=side,
            quantity=quantity,
            strategy_id=strategy_id,
        )

    sells = [order(t, "sell", -q) for t, q in trades.items() if q < 0]
    buys = [order(t, "buy", q * scale) for t, q in trades.items() if q > 0 and q * scale > 0]
    return sells + buys


def _long_short_orders(
    target_weights: Mapping[str, float],
    portfolio: Portfolio,
    prices: Mapping[str, float],
    buffer_fraction: float,
    min_trade_weight: float,
    *,
    as_of: date,
    strategy_id: str,
) -> list[Order]:
    """The ``allow_short`` route of :func:`orders_from_targets`."""
    bad = {t: w for t, w in target_weights.items() if not math.isfinite(w)}
    if bad:
        raise ValueError(f"targets must be finite: {bad}")
    blind = [t for t, q in portfolio.positions.items() if q < 0 and _price(prices, t) is None]
    if blind:
        # an unpriced short is a liability of unknown size: counting it as 0
        # would overstate equity and oversize every target (BE-45)
        _log.warning("orders.unpriced_short_holdings", tickers=sorted(blind))
        return []
    equity = portfolio.total_value(prices)
    if not equity > 0:
        return []

    trades: dict[str, float] = {}  # ticker -> signed quantity
    unpriced: list[str] = []
    for ticker in sorted({*portfolio.positions, *target_weights}):
        price = _price(prices, ticker)
        if price is None:
            unpriced.append(ticker)
            continue
        held = portfolio.positions.get(ticker, 0.0)
        target = float(target_weights.get(ticker, 0.0))
        if target == 0.0:
            if held != 0.0:
                trades[ticker] = -held  # full exit, exact quantity
            continue
        current = held * price / equity
        flip = held != 0.0 and (held > 0) != (target > 0)
        band = buffer_fraction * abs(target)
        if not flip and abs(current - target) <= band:
            continue
        edge = target - band if current < target else target + band
        delta = edge - current
        if not flip and abs(delta) < min_trade_weight:
            continue
        trades[ticker] = delta * equity / price
    if unpriced:
        _log.warning("portfolio.orders.unpriced.skipped", tickers=unpriced)

    legs: list[Order] = []
    for ticker, qty in trades.items():
        side: OrderSide = "buy" if qty > 0 else "sell"
        order = Order(
            client_id=make_client_id(
                as_of=as_of, strategy_id=strategy_id, ticker=ticker, side=side
            ),
            ticker=ticker,
            side=side,
            quantity=abs(qty),
            strategy_id=strategy_id,
        )
        legs.extend(classify(order, portfolio.positions.get(ticker, 0.0)))

    proceeds = sum(o.quantity * prices[o.ticker] for o in legs if o.side == "sell")
    covers = sum(
        o.quantity * prices[o.ticker]
        for o in legs
        if o.side == "buy" and o.position_effect == "close"
    )
    spend = sum(
        o.quantity * prices[o.ticker]
        for o in legs
        if o.side == "buy" and o.position_effect == "open"
    )
    budget = max(portfolio.cash + proceeds - covers, 0.0)
    scale = 1.0 if spend <= budget else budget / spend
    if scale < 1.0:
        _log.info("portfolio.orders.buys_scaled", scale=scale, spend=spend, budget=budget)
    out: list[Order] = []
    for o in legs:
        if o.side == "buy" and o.position_effect == "open" and scale < 1.0:
            if o.quantity * scale <= 0:
                continue
            o = replace(o, quantity=o.quantity * scale)
        out.append(o)
    return [o for o in out if o.side == "sell"] + [o for o in out if o.side == "buy"]
