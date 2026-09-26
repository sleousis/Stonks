"""Risk layer between ``strategy.decide`` and the broker (roadmap 2.3).

``apply_risk`` takes a strategy's proposed orders and returns the orders
that are allowed to reach the broker, plus one ``RiskAdjustment`` per order
it clipped or dropped. Rules only ever *reduce* exposure:

- sells are never blocked; they are only clipped to the held quantity so a
  sell cannot flip into a short, and they are placed before buys so their
  freed slots and proceeds are real by the time buys are placed;
- buys are clipped, in order, by ``max_open_positions``,
  ``max_weight_per_ticker``, ``max_weight_per_asset_class`` and the cash
  buffer (net of slippage and fees), then dropped if their notional falls
  below ``min_order_notional``.

All weights are measured against the portfolio value before the tick's
orders. The function is pure: the portfolio passed in is not mutated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.logging import get_logger

__all__ = ["RiskAdjustment", "RiskPolicy", "RiskResult", "apply_risk"]

RiskRule = Literal[
    "sell_exceeds_position",
    "no_price",
    "max_open_positions",
    "max_weight_per_ticker",
    "unknown_asset_class",
    "unpriced_holding",
    "max_weight_per_asset_class",
    "cash_buffer",
    "min_order_notional",
]

_log = get_logger("stonks.production.risk")

# Quantities below this are treated as zero (float noise from clipping).
_EPS = 1e-9


@dataclass(frozen=True)
class RiskAdjustment:
    ticker: str
    side: str
    rule: RiskRule
    original_quantity: float
    adjusted_quantity: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "rule": self.rule,
            "original_quantity": self.original_quantity,
            "adjusted_quantity": self.adjusted_quantity,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RiskResult:
    orders: list[Order]
    adjustments: list[RiskAdjustment]


def apply_risk(
    orders: Sequence[Order],
    portfolio: Portfolio,
    prices: Mapping[str, float],
    asset_classes: Mapping[str, str],
    policy: RiskPolicy,
    *,
    slippage_bps: float = 0.0,
    fee_per_trade: float = 0.0,
) -> RiskResult:
    if not policy.enabled:
        return RiskResult(orders=list(orders), adjustments=[])

    adjustments: list[RiskAdjustment] = []
    equity = portfolio.total_value(prices)
    positions = dict(portfolio.positions)
    cash = portfolio.cash
    kept: list[Order] = []

    def record(order: Order, rule: RiskRule, new_qty: float, reason: str) -> None:
        adj = RiskAdjustment(
            ticker=order.ticker,
            side=order.side,
            rule=rule,
            original_quantity=order.quantity,
            adjusted_quantity=max(new_qty, 0.0),
            reason=reason,
        )
        adjustments.append(adj)
        _log.info("risk.adjusted", client_id=order.client_id, **adj.as_dict())

    # ---- sells first: never blocked, only clipped to the held quantity ----
    for order in (o for o in orders if o.side == "sell"):
        held = positions.get(order.ticker, 0.0)
        qty = order.quantity
        if qty > held:
            record(
                order,
                "sell_exceeds_position",
                held,
                f"sell of {qty} exceeds held {held}; clipped so it cannot open a short",
            )
            qty = held
        if qty <= _EPS:
            continue
        kept.append(order if qty == order.quantity else replace(order, quantity=qty))
        remaining = held - qty
        if remaining <= _EPS:
            positions.pop(order.ticker, None)
        else:
            positions[order.ticker] = remaining
        price = prices.get(order.ticker)
        if price and price > 0:
            cash += qty * price * (1 - slippage_bps / 10_000.0) - fee_per_trade

    # ---- buys: clipped by each rule in turn ----
    for order in (o for o in orders if o.side == "buy"):
        price = prices.get(order.ticker)
        if not price or price <= 0:
            record(order, "no_price", 0.0, "no current price; cannot size the order")
            continue
        qty = order.quantity
        held = positions.get(order.ticker, 0.0)

        # max open positions: only a buy that opens a new position counts.
        if policy.max_open_positions is not None and held <= _EPS:
            open_count = sum(1 for q in positions.values() if q > _EPS)
            if open_count >= policy.max_open_positions:
                record(
                    order,
                    "max_open_positions",
                    0.0,
                    f"{open_count} open positions >= limit {policy.max_open_positions}",
                )
                continue

        # per-ticker weight.
        room = policy.max_weight_per_ticker * equity - max(held, 0.0) * price
        qty = _clip(order, qty, room / price, "max_weight_per_ticker", record)
        if qty is None:
            continue

        # per-asset-class weight.
        if policy.max_weight_per_asset_class:
            cls = asset_classes.get(order.ticker)
            if cls is None:
                record(
                    order,
                    "unknown_asset_class",
                    0.0,
                    "asset class unknown while asset-class caps are configured",
                )
                continue
            cap = policy.max_weight_per_asset_class.get(cls)
            if cap is not None:
                unpriced = sorted(
                    t
                    for t, q in positions.items()
                    if q > 0 and asset_classes.get(t) == cls and not prices.get(t)
                )
                if unpriced:
                    record(
                        order,
                        "unpriced_holding",
                        0.0,
                        f"{cls} exposure unknown: no price for held {', '.join(unpriced)}",
                    )
                    continue
                class_value = sum(
                    q * prices.get(t, 0.0)
                    for t, q in positions.items()
                    if q > 0 and asset_classes.get(t) == cls
                )
                room = cap * equity - class_value
                qty = _clip(order, qty, room / price, "max_weight_per_asset_class", record)
                if qty is None:
                    continue

        # cash buffer, net of slippage and the flat fee.
        fill_price = price * (1 + slippage_bps / 10_000.0)
        spendable = cash - policy.cash_buffer_fraction * equity - fee_per_trade
        qty = _clip(order, qty, spendable / fill_price, "cash_buffer", record)
        if qty is None:
            continue

        # min notional, after all clipping.
        if qty * price < policy.min_order_notional:
            record(
                order,
                "min_order_notional",
                0.0,
                f"notional {qty * price:.2f} < min {policy.min_order_notional}",
            )
            continue

        kept.append(order if qty == order.quantity else replace(order, quantity=qty))
        positions[order.ticker] = held + qty
        cash -= qty * fill_price + fee_per_trade

    return RiskResult(orders=kept, adjustments=adjustments)


def _clip(
    order: Order,
    qty: float,
    max_qty: float,
    rule: RiskRule,
    record: Any,
) -> float | None:
    """Clip ``qty`` to ``max_qty``; return None when nothing is left."""
    if qty <= max_qty:
        return qty
    new_qty = max(max_qty, 0.0)
    record(order, rule, new_qty, f"quantity {qty} exceeds {rule} room {new_qty}")
    if new_qty <= _EPS:
        return None
    return new_qty
