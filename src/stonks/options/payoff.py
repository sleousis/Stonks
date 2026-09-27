"""Expiry payoff of an options structure (roadmap 17.6).

:func:`expiry_payoff` values a set of legs at expiry over a range of
underlying prices, less what they cost to open: the curve the console
draws, with the max loss, max gain and breakevens. The payoff is
piecewise linear with kinks at the strikes, so those figures are exact,
not read off the grid.

:func:`structure_payoff` builds one structure from a day's chain with the
structure registry (the same builders the options backtest uses) and
returns its legs and payoff. Research only: nothing here trades.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from stonks.backtest.options_ledger import OptionLedger
from stonks.core.options import OptionContract
from stonks.options.chain import ChainSnapshot
from stonks.options.selector import LegSelector
from stonks.options.strategy import OptionDecisionContext, OptionIntent
from stonks.options.structures import BuildRequest, build, structures

#: Structures written against shares held: their payoff includes one
#: contract's worth of shares (a covered call is shares plus a short call).
HOLDS_SHARES = frozenset({"covered_call", "protective_put"})

#: Structures that exit a position group rather than open one.
EXIT_STRUCTURES = frozenset({"close_group"})

#: Parameters each structure reads from its intent (``dte`` for all).
_PARAMS: dict[str, tuple[str, ...]] = {
    "long_call": ("dte", "delta"),
    "long_put": ("dte", "delta"),
    "covered_call": ("dte", "delta"),
    "cash_secured_put": ("dte", "delta"),
    "protective_put": ("dte", "delta"),
    "bull_call_spread": ("dte", "long_delta", "short_delta"),
    "bear_put_spread": ("dte", "long_delta", "short_delta"),
    "bull_put_spread": ("dte", "long_delta", "short_delta"),
    "bear_call_spread": ("dte", "long_delta", "short_delta"),
    "iron_condor": ("dte", "short_delta", "wing_delta"),
}

#: The widest expiry window a payoff may pick from (days).
MAX_DTE = 400


@dataclass(frozen=True)
class PayoffLeg:
    """One leg: an option ``contract`` or ``shares`` of a ticker, a signed
    ``quantity`` (contracts or shares) and its ``price`` now (per share)."""

    contract: OptionContract | None
    quantity: float
    price: float
    shares: str | None = None

    def value_at(self, spot: float) -> float:
        if self.contract is None:
            return self.quantity * spot
        return self.quantity * self.contract.multiplier * self.contract.intrinsic(spot)

    @property
    def cost(self) -> float:
        size = 1.0 if self.contract is None else self.contract.multiplier
        return self.quantity * size * self.price

    @property
    def slope_above(self) -> float:
        """Change in value per dollar of the underlying above every strike."""
        if self.contract is None:
            return self.quantity
        return self.quantity * self.contract.multiplier if self.contract.is_call else 0.0


@dataclass(frozen=True)
class Payoff:
    #: ``(underlying price at expiry, profit)``, sorted by price.
    points: tuple[tuple[float, float], ...]
    #: Paid to open, in money: positive for a debit, negative for a credit.
    cost: float
    #: The most it can lose (a positive number), ``None`` when unbounded.
    max_loss: float | None
    #: The most it can make, ``None`` when unbounded.
    max_gain: float | None
    breakevens: tuple[float, ...]


def _profit(legs: Sequence[PayoffLeg], spot: float, cost: float) -> float:
    return sum(leg.value_at(spot) for leg in legs) - cost


def expiry_payoff(legs: Sequence[PayoffLeg], *, spot: float, n_points: int = 81) -> Payoff:
    """The profit of ``legs`` at expiry, over prices around ``spot`` and the
    strikes. Legs of different expiries are all valued at intrinsic."""
    if not legs:
        raise ValueError("a payoff needs at least one leg")
    cost = sum(leg.cost for leg in legs)
    strikes = sorted({leg.contract.strike for leg in legs if leg.contract is not None})
    slope_up = sum(leg.slope_above for leg in legs)
    anchors = [*strikes, spot]
    far = max(anchors) * 3.0
    kinks = [0.0, *strikes, far]
    values = [_profit(legs, s, cost) for s in kinks]
    lowest, highest = min(values), max(values)
    breakevens: list[float] = []
    for (a, va), (b, vb) in zip(zip(kinks, values), zip(kinks[1:], values[1:]), strict=False):
        if va == 0.0 and a > 0:
            breakevens.append(a)
        elif va * vb < 0:
            breakevens.append(a + (b - a) * (-va) / (vb - va))
    if slope_up > 1e-9:
        tail = far + (-values[-1] / slope_up if values[-1] < 0 else 0.0)
        if values[-1] < 0:
            breakevens.append(tail)
    lo = max(0.0, min(anchors) * 0.7)
    hi = max(anchors) * 1.3
    step = (hi - lo) / max(n_points - 1, 1)
    grid = {round(lo + i * step, 2) for i in range(n_points)}
    grid |= {round(s, 2) for s in [*strikes, spot, *breakevens] if lo <= s <= hi}
    points = tuple((s, _profit(legs, s, cost)) for s in sorted(grid))
    return Payoff(
        points=points,
        cost=cost,
        max_loss=None if slope_up < -1e-9 else max(-lowest, 0.0),
        max_gain=None if slope_up > 1e-9 else max(highest, 0.0),
        breakevens=tuple(round(b, 4) for b in sorted(set(breakevens))),
    )


@dataclass(frozen=True)
class StructureInfo:
    name: str
    params: tuple[str, ...]
    holds_shares: bool


def payoff_structures() -> list[StructureInfo]:
    """Every registered structure that opens a position."""
    return [
        StructureInfo(name, _PARAMS.get(name, ("dte",)), name in HOLDS_SHARES)
        for name in structures()
        if name not in EXIT_STRUCTURES
    ]


@dataclass(frozen=True)
class StructurePayoff:
    structure: str
    legs: tuple[PayoffLeg, ...]
    payoff: Payoff


def structure_payoff(
    structure: str,
    chain: ChainSnapshot,
    params: Mapping[str, Any],
    *,
    selector: LegSelector | None = None,
) -> StructurePayoff | None:
    """One unit of ``structure`` built from ``chain`` and its payoff;
    ``None`` when the chain cannot supply the legs."""
    if structure in EXIT_STRUCTURES or structure not in structures():
        raise ValueError(f"unknown structure {structure!r}")
    spot = chain.spot
    if spot is None or not len(chain):
        return None
    ctx = OptionDecisionContext(
        as_of=chain.as_of,
        history={chain.underlying: [(chain.as_of, spot)]},
        chains={chain.underlying: chain},
        ledger=OptionLedger(cash=0.0),
        equity=0.0,
    )
    request = BuildRequest(
        intent=OptionIntent(chain.underlying, structure, dict(params), quantity=1),
        ctx=ctx,
        selector=selector or LegSelector(min_dte=1, max_dte=MAX_DTE),
        client_id=f"payoff:{structure}",
    )
    combo = build(request)
    if combo is None:
        return None
    marks = {q.contract_id: q.mark for q in chain}
    legs: list[PayoffLeg] = []
    for leg in combo.legs:
        qty = leg.sign * leg.ratio * combo.quantity
        if leg.contract is None:
            legs.append(PayoffLeg(None, qty, spot, shares=leg.shares))
            continue
        mark = marks.get(leg.contract.contract_id)
        if mark is None or not math.isfinite(mark):
            return None
        legs.append(PayoffLeg(leg.contract, qty, mark))
    if structure in HOLDS_SHARES:
        multiplier = next((leg.contract.multiplier for leg in legs if leg.contract), 100.0)
        legs.insert(0, PayoffLeg(None, multiplier, spot, shares=chain.underlying))
    return StructurePayoff(structure, tuple(legs), expiry_payoff(legs, spot=spot))
