"""The rebalancing planner (roadmap 23.16): the trades that move a book to
target weights, shown before anything is sent.

Pure: the book, prices, targets, a cost model and optional open tax lots in,
a :class:`RebalancePlan` out. Nothing is written or sent. The service layer
(``app/planner.py``) loads the inputs and, on a confirm, turns the plan's
trades into order tickets.

Rules, per ticker held or targeted, with ``E`` the book's value at the
given prices:

- the target holding is ``floor(weight * E / price)`` whole shares (never
  a fraction), so a small weight on a dear share can round to zero;
- a held ticker missing from the targets is sold in full;
- weights are long only and may not add to more than 1 (the rest is cash);
- sells come first, then buys, each in ticker order. Buys are cut back,
  largest first, by whole shares until they fit the cash plus the sells'
  proceeds less every trade's estimated cost;
- a trade below ``min_trade_value`` is skipped (a full exit never is);
- each trade's estimated cost is the cost model's, in bps of its value
  and in currency. ``turnover`` is the traded value over ``E``;
- with open lots, each sell shows the lots it closes (oldest first), the
  gain and whether it is short or long term. With a ``tax_rate`` per
  holding period it also shows the estimated tax. It is a preview: the
  broker's lots and the tax report decide.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from stonks.backtest.costs import CostModel, Trade
from stonks.core.types import AssetClass, OrderSide
from stonks.tax.lots import HoldingPeriod, OpenLot

_EPS = 1e-9
_BPS = 10_000.0

SkipReason = Literal["no_price", "below_min_trade", "below_one_share", "no_cash"]


class PlanError(ValueError):
    """Targets or a book the planner cannot plan for."""


@dataclass(frozen=True)
class LotSale:
    """One lot a sell closes, oldest first."""

    acquired: date
    quantity: float
    cost_basis: float
    proceeds: float
    gain: float
    holding_period: HoldingPeriod


@dataclass(frozen=True)
class TaxPreview:
    lots: tuple[LotSale, ...]
    gain: float
    short_term_gain: float
    long_term_gain: float
    #: ``None`` when no tax rate was given.
    estimated_tax: float | None


@dataclass(frozen=True)
class PlanLine:
    """One ticker's trade (or why it has none)."""

    ticker: str
    price: float | None
    current_quantity: float
    target_weight: float
    current_weight: float
    target_quantity: float
    side: OrderSide | None
    quantity: float
    value: float
    cost_bps: float | None
    cost: float
    weight_after: float
    skipped: SkipReason | None = None
    tax: TaxPreview | None = None

    @property
    def trades(self) -> bool:
        return self.side is not None and self.quantity > 0


@dataclass(frozen=True)
class RebalancePlan:
    as_of: date
    equity: float
    cash_before: float
    cash_after: float
    lines: tuple[PlanLine, ...]
    turnover: float
    total_cost: float
    buys: float
    sells: float
    #: Target weights left as cash by whole shares and cut buys.
    cash_weight_after: float
    #: The largest gap between a target and the weight after, in weight.
    max_drift_after: float
    notes: tuple[str, ...] = field(default=())

    @property
    def trades(self) -> tuple[PlanLine, ...]:
        return tuple(line for line in self.lines if line.trades)

    @property
    def tax_total(self) -> float | None:
        taxes = [line.tax.estimated_tax for line in self.lines if line.tax is not None]
        known = [t for t in taxes if t is not None]
        return sum(known) if known else None


def _price(prices: Mapping[str, float], ticker: str) -> float | None:
    p = prices.get(ticker)
    return float(p) if p is not None and math.isfinite(p) and p > 0 else None


def _check_targets(targets: Mapping[str, float]) -> None:
    for ticker, weight in targets.items():
        if not math.isfinite(weight) or weight < 0:
            raise PlanError(f"{ticker}: a target weight must be a number >= 0 (long only)")
    total = sum(targets.values())
    if total > 1.0 + 1e-6:
        raise PlanError(f"target weights add to {total:.4f}, above 1")


def plan_rebalance(
    *,
    as_of: date,
    cash: float,
    positions: Mapping[str, float],
    prices: Mapping[str, float],
    targets: Mapping[str, float],
    cost_model: CostModel | None = None,
    asset_classes: Mapping[str, AssetClass] | None = None,
    min_trade_value: float = 0.0,
    lots: Mapping[str, Sequence[OpenLot]] | None = None,
    tax_rates: Mapping[HoldingPeriod, float] | None = None,
) -> RebalancePlan:
    """The trades that move ``positions`` and ``cash`` to ``targets`` (see
    the module doc). Raises :class:`PlanError`."""
    _check_targets(targets)
    if min_trade_value < 0:
        raise PlanError("min_trade_value must be >= 0")
    for ticker, qty in positions.items():
        if qty < -_EPS:
            raise PlanError(f"{ticker}: the planner does not plan short books")
    notes: list[str] = []
    valued = {t: q for t, q in positions.items() if abs(q) > _EPS}
    unpriced = sorted(t for t in valued if _price(prices, t) is None)
    if unpriced:
        notes.append(f"no price for {', '.join(unpriced)}: left as they are and out of the value")
    equity = cash + sum(q * (_price(prices, t) or 0.0) for t, q in valued.items())
    if equity <= 0:
        raise PlanError("the book has no value to plan with")

    classes = asset_classes or {}
    draft: dict[str, dict[str, object]] = {}
    for ticker in sorted(set(valued) | {t for t, w in targets.items() if w > 0}):
        held = float(valued.get(ticker, 0.0))
        price = _price(prices, ticker)
        weight = float(targets.get(ticker, 0.0))
        if price is None:
            draft[ticker] = {"price": None, "held": held, "weight": weight, "target": held,
                             "skip": "no_price"}  # fmt: skip
            continue
        target = float(math.floor(weight * equity / price + _EPS))
        skip: SkipReason | None = None
        if weight > 0 and target < 1 and held < 1:
            skip = "below_one_share"
        draft[ticker] = {"price": price, "held": held, "weight": weight, "target": target,
                         "skip": skip}  # fmt: skip

    def cost_of(
        ticker: str, side: OrderSide, qty: float, price: float
    ) -> tuple[float | None, float]:
        if cost_model is None or qty <= 0:
            return None, 0.0
        trade = Trade(ticker=ticker, side=side, quantity=qty, price=price,
                      asset_class=classes.get(ticker, "equity"))  # fmt: skip
        tc = cost_model.cost(trade)
        adverse = (tc.fill_price - price) * qty if side == "buy" else (price - tc.fill_price) * qty
        total = adverse + tc.fee
        return total / (qty * price) * _BPS, total

    # sells, then buys scaled to the cash they leave
    orders: dict[str, tuple[OrderSide, float]] = {}
    for ticker, d in draft.items():
        if d["price"] is None:
            continue
        delta = float(d["target"]) - float(d["held"])  # type: ignore[arg-type]
        if abs(delta) < _EPS:
            continue
        side: OrderSide = "buy" if delta > 0 else "sell"
        qty = abs(delta) if side == "buy" else min(abs(delta), float(d["held"]))  # type: ignore[arg-type]
        full_exit = side == "sell" and float(d["target"]) <= _EPS  # type: ignore[arg-type]
        if side == "sell" and not full_exit:
            qty = float(math.floor(qty + _EPS))
        if qty <= 0:
            continue
        value = qty * float(d["price"])  # type: ignore[arg-type]
        if not full_exit and value < min_trade_value:
            d["skip"] = "below_min_trade"
            continue
        orders[ticker] = (side, qty)

    def budget() -> float:
        spend = cash
        for ticker, (side, qty) in orders.items():
            price = float(draft[ticker]["price"])  # type: ignore[arg-type]
            cost = cost_of(ticker, side, qty, price)[1]
            spend += (qty * price if side == "sell" else -qty * price) - cost
        return spend

    cut = False
    while budget() < -_EPS:
        buys = [(qty * float(draft[t]["price"]), t) for t, (s, qty) in orders.items() if s == "buy"]  # type: ignore[arg-type]
        if not buys:
            break
        _, ticker = max(buys)
        side, qty = orders[ticker]
        cut = True
        if qty <= 1 + _EPS:
            del orders[ticker]
            draft[ticker]["skip"] = "no_cash"
        else:
            orders[ticker] = (side, qty - 1.0)
    if cut:
        notes.append("buys were cut by whole shares to fit the cash")

    lines: list[PlanLine] = []
    cash_after = cash
    total_cost = buys_value = sells_value = 0.0
    held_after: dict[str, float] = {}
    for ticker in sorted(draft, key=lambda t: (orders.get(t, ("x",))[0] != "sell", t)):
        d = draft[ticker]
        price = d["price"]
        held = float(d["held"])  # type: ignore[arg-type]
        side_qty = orders.get(ticker)
        cost_bps: float | None = None
        cost = value = 0.0
        tax: TaxPreview | None = None
        if side_qty is not None and price is not None:
            side, qty = side_qty
            value = qty * float(price)  # type: ignore[arg-type]
            cost_bps, cost = cost_of(ticker, side, qty, float(price))  # type: ignore[arg-type]
            total_cost += cost
            if side == "buy":
                buys_value += value
                cash_after -= value + cost
                held_after[ticker] = held + qty
            else:
                sells_value += value
                cash_after += value - cost
                held_after[ticker] = held - qty
                if lots is not None:
                    tax = tax_preview(lots.get(ticker, ()), qty, float(price), as_of, tax_rates)  # type: ignore[arg-type]
        else:
            held_after[ticker] = held
        lines.append(
            PlanLine(
                ticker=ticker,
                price=price,  # type: ignore[arg-type]
                current_quantity=held,
                target_weight=float(d["weight"]),  # type: ignore[arg-type]
                current_weight=held * float(price) / equity if price is not None else 0.0,  # type: ignore[arg-type]
                target_quantity=float(d["target"]),  # type: ignore[arg-type]
                side=side_qty[0] if side_qty else None,
                quantity=side_qty[1] if side_qty else 0.0,
                value=value,
                cost_bps=cost_bps,
                cost=cost,
                weight_after=0.0,
                skipped=d["skip"] if side_qty is None else None,  # type: ignore[arg-type]
                tax=tax,
            )
        )
    after = [
        PlanLine(**{**line.__dict__, "weight_after": (
            held_after[line.ticker] * line.price / equity if line.price is not None else 0.0)})
        for line in lines
    ]  # fmt: skip
    drift = max((abs(line.weight_after - line.target_weight) for line in after
                 if line.price is not None), default=0.0)  # fmt: skip
    return RebalancePlan(
        as_of=as_of,
        equity=equity,
        cash_before=cash,
        cash_after=cash_after,
        lines=tuple(after),
        turnover=(buys_value + sells_value) / equity,
        total_cost=total_cost,
        buys=buys_value,
        sells=sells_value,
        cash_weight_after=cash_after / equity,
        max_drift_after=drift,
        notes=tuple(notes),
    )


def tax_preview(
    lots: Sequence[OpenLot],
    quantity: float,
    price: float,
    as_of: date,
    rates: Mapping[HoldingPeriod, float] | None = None,
) -> TaxPreview:
    """The long lots a sell of ``quantity`` at ``price`` closes, oldest
    first, with the gain and, given ``rates``, the tax."""
    left = quantity
    sales: list[LotSale] = []
    for lot in sorted((x for x in lots if x.kind == "long"), key=lambda x: x.acquired):
        if left <= _EPS:
            break
        take = min(left, lot.quantity)
        basis = lot.cost_basis * take / lot.quantity
        proceeds = take * price
        sales.append(
            LotSale(
                acquired=lot.acquired,
                quantity=take,
                cost_basis=basis,
                proceeds=proceeds,
                gain=proceeds - basis,
                holding_period=lot.holding_period(as_of),
            )
        )
        left -= take
    short = sum(s.gain for s in sales if s.holding_period == "short")
    long_ = sum(s.gain for s in sales if s.holding_period == "long")
    tax: float | None = None
    if rates is not None:
        tax = max(0.0, short) * rates.get("short", 0.0) + max(0.0, long_) * rates.get("long", 0.0)
    return TaxPreview(
        lots=tuple(sales),
        gain=short + long_,
        short_term_gain=short,
        long_term_gain=long_,
        estimated_tax=tax,
    )
