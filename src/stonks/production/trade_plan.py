"""The trade plan on the manual ticket (roadmap 23.4). Pure.

A plan is an entry, a stop and a target. The stop sits on the losing side
of the entry (below a long, above a short) and the target on the winning
side. The size comes from the risk the person chooses, as a percent of the
book or as an amount, divided by the distance to the stop, and is rounded
down to whole shares: the loss at the stop never exceeds the chosen risk
(P27). A buy is also capped by the cash on hand.

A stop fills at the market after a gap, so the risk here is the planned
loss, not the worst case.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from stonks.core.types import OrderSide

__all__ = [
    "PlanError",
    "PlanSize",
    "check_plan",
    "reward_risk",
    "risk_per_share",
    "size_from_risk",
]

_EPS = 1e-9


class PlanError(ValueError):
    """The plan does not make sense (a stop on the wrong side, no risk)."""


def check_plan(side: OrderSide, entry: float, *, stop: float | None, target: float | None) -> None:
    """Refuse a stop or a target on the wrong side of ``entry``."""
    if not entry > 0:
        raise PlanError("the entry price must be positive")
    long = side == "buy"
    if stop is not None:
        if not stop > 0:
            raise PlanError("the stop must be positive")
        if long and stop >= entry:
            raise PlanError(f"the stop of a buy must be below the entry {entry:g}")
        if not long and stop <= entry:
            raise PlanError(f"the stop of a short sale must be above the entry {entry:g}")
    if target is not None:
        if not target > 0:
            raise PlanError("the target must be positive")
        if long and target <= entry:
            raise PlanError(f"the target of a buy must be above the entry {entry:g}")
        if not long and target >= entry:
            raise PlanError(f"the target of a short sale must be below the entry {entry:g}")


def risk_per_share(side: OrderSide, entry: float, stop: float) -> float:
    """What one share loses if the stop fills at its price."""
    return entry - stop if side == "buy" else stop - entry


def reward_risk(side: OrderSide, entry: float, stop: float, target: float | None) -> float | None:
    """The gain at the target over the loss at the stop."""
    if target is None:
        return None
    risk = risk_per_share(side, entry, stop)
    if risk <= _EPS:
        return None
    reward = target - entry if side == "buy" else entry - target
    return reward / risk


@dataclass(frozen=True)
class PlanSize:
    #: Whole shares.
    quantity: int
    #: The risk chosen, in money.
    risk_budget: float
    risk_per_share: float
    #: The loss at the stop for ``quantity`` (at most ``risk_budget``).
    risk_amount: float
    notional: float
    reward_risk: float | None
    #: ``cash`` when the cash on hand cut the size.
    capped_by: Literal["cash"] | None = None
    #: Why the size is what it is, when it needs saying.
    note: str | None = None


def size_from_risk(
    *,
    side: OrderSide,
    entry: float,
    stop: float,
    equity: float,
    risk_percent: float | None = None,
    risk_amount: float | None = None,
    cash: float | None = None,
    target: float | None = None,
) -> PlanSize:
    """Whole shares whose loss at ``stop`` fits the chosen risk:
    ``risk_percent`` of ``equity`` or ``risk_amount``, exactly one of them.
    A buy is capped at what ``cash`` pays for."""
    if (risk_percent is None) == (risk_amount is None):
        raise PlanError("give one of risk_percent or risk_amount")
    check_plan(side, entry, stop=stop, target=target)
    budget = equity * risk_percent / 100.0 if risk_percent is not None else float(risk_amount or 0)
    if not budget > 0:
        raise PlanError("the risk must be above zero")
    per_share = risk_per_share(side, entry, stop)
    quantity = max(0, math.floor(budget / per_share + _EPS))
    capped: Literal["cash"] | None = None
    if side == "buy" and cash is not None:
        affordable = max(0, math.floor(max(cash, 0.0) / entry + _EPS))
        if affordable < quantity:
            quantity, capped = affordable, "cash"
    note = None
    if quantity == 0:
        note = (
            "not enough cash for one share"
            if capped == "cash"
            else f"the risk {budget:,.2f} is less than one share risks ({per_share:,.2f})"
        )
    return PlanSize(
        quantity=quantity,
        risk_budget=budget,
        risk_per_share=per_share,
        risk_amount=quantity * per_share,
        notional=quantity * entry,
        reward_risk=reward_risk(side, entry, stop, target),
        capped_by=capped,
        note=note,
    )
