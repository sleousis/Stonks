"""Live option combos as ledger orders and back (roadmap 17.8).

A combo is written as one ledger order (and one ticket) per leg, so the
position ledger, reconciliation and the approval queue work as for any
order. Each leg order carries what rebuilds its combo at submit time in
its ``decision_context``: the combo id, the structure, the net limit, the
number of combo units, the leg's ratio and position, and how many legs
the combo has. A leg's own ``limit_price`` is its limit at the mid, shown
on the ticket. A one-leg combo is sent as that leg's order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from stonks.core.combos import ComboLeg, ComboOrder
from stonks.core.options import is_option_id, parse_contract_id
from stonks.core.types import Order
from stonks.options.live.risk import leg_orders


def to_leg_orders(
    combo: ComboOrder,
    held: Mapping[str, float],
    leg_limits: Mapping[str, float],
    *,
    tick_id: str | None = None,
) -> list[Order]:
    """The ledger orders of ``combo``: day limits at each leg's mid."""
    if combo.net_limit is None:
        raise ValueError(f"{combo.client_id}: price the combo before writing its legs")
    out: list[Order] = []
    for i, (leg, order) in enumerate(zip(combo.legs, leg_orders(combo, held), strict=True)):
        limit = round(leg_limits[leg.instrument], 6)
        context = dict(order.decision_context or {})
        context.update(
            {
                "combo_id": combo.client_id,
                "structure": combo.structure,
                "net_limit": combo.net_limit,
                "combo_quantity": combo.quantity,
                "combo_effect": combo.effect,
                "leg_index": i,
                "leg_ratio": leg.ratio,
                "legs": len(combo.legs),
                "multiplier": leg.contract.multiplier if leg.contract is not None else 1.0,
                "reason": combo.reason,
            }
        )
        out.append(
            Order(
                client_id=order.client_id,
                ticker=order.ticker,
                side=order.side,
                quantity=order.quantity,
                order_type="limit",
                limit_price=max(limit, 0.01),
                time_in_force="day",
                decision_price=limit,
                strategy_id=combo.strategy_id,
                tick_id=tick_id,
                position_effect=order.position_effect,
                decision_context=context,
            )
        )
    return out


def is_option_leg(order: Order) -> bool:
    return is_option_id(order.ticker)


def combo_size(order: Order) -> int:
    """How many legs the order's combo has (1 for a plain order)."""
    ctx = order.decision_context or {}
    try:
        return int(ctx.get("legs") or 1)
    except (TypeError, ValueError):
        return 1


def combo_from_legs(orders: Sequence[Order]) -> ComboOrder:
    """The combo the leg orders were written from (in leg order)."""
    if not orders:
        raise ValueError("a combo needs its legs")
    legs = sorted(orders, key=lambda o: int((o.decision_context or {}).get("leg_index", 0)))
    ctx = legs[0].decision_context or {}
    combo_id = str(ctx.get("combo_id") or "")
    if not combo_id or any((o.decision_context or {}).get("combo_id") != combo_id for o in legs):
        raise ValueError("the legs belong to different combos")
    if len(legs) != combo_size(legs[0]):
        raise ValueError(f"{combo_id}: {len(legs)} of {combo_size(legs[0])} legs")
    combo_legs = []
    for o in legs:
        c = o.decision_context or {}
        ratio = float(c.get("leg_ratio") or 1.0)
        if is_option_id(o.ticker):
            combo_legs.append(ComboLeg(o.side, ratio, contract=parse_contract_id(o.ticker)))
        else:
            combo_legs.append(ComboLeg(o.side, ratio, shares=o.ticker))
    effect = str(ctx.get("combo_effect") or "open")
    return ComboOrder(
        client_id=combo_id,
        legs=tuple(combo_legs),
        quantity=float(ctx.get("combo_quantity") or 1.0),
        structure=str(ctx.get("structure") or "custom"),
        net_limit=float(ctx["net_limit"]) if ctx.get("net_limit") is not None else None,
        effect="close" if effect == "close" else "open",
        strategy_id=legs[0].strategy_id,
        reason=str(ctx.get("reason") or ""),
    )
