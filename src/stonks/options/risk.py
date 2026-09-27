"""Option risk analytics (roadmap 17.4): position and portfolio Greeks,
the max loss of a structure, and two margin estimates.

Positions are ``instrument -> signed quantity``: contracts for an option
(keyed by contract id) and shares for a stock. Money figures use the
contract multiplier.

Greeks
------
``dollar_delta`` is ``sum(delta x multiplier x qty x spot)`` plus shares x
spot. ``dollar_gamma`` is the change in dollar delta for a 1% move
(``gamma x multiplier x qty x spot^2 / 100``). ``vega`` is dollars per vol
point, ``theta`` dollars per calendar day.

Max loss
--------
The expiry payoff of a set of legs is piecewise linear in the underlying
price with kinks at the strikes, so its minimum sits at zero, at a strike,
or runs off to minus infinity when the slope above the last strike is
negative (a naked short call). ``max_loss`` is the worst payoff minus
what the legs are worth now, so a new structure's max loss includes the
premium paid, and a credit received offsets it. Legs of different expiries
are all valued at intrinsic, an approximation for calendars.

Margin
------
- ``reg_t_requirement``: strategy-based, Reg T style. The requirement is
  the worst expiry value of the legs taken together (so a vertical needs
  its width, an iron condor its wider wing, a cash-secured put its strike,
  a covered call nothing beyond the shares). A leg set with unbounded loss
  (a naked short call) needs the naked formula: premium plus the larger
  of 20% of the underlying less the out-of-the-money amount and 10% of
  the underlying (15% and 10% for cash-settled index options).
- ``risk_based_requirement``: a simple portfolio-margin style estimate. It
  reprices the legs at underlying moves from -15% to +15% (index options
  -8% to +6%, as in the OCC's TIMS ranges) and takes the worst loss, with a
  floor of 0.375 x multiplier per short contract.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

from stonks.core.options import OptionContract, is_option_id, parse_contract_id
from stonks.options.pricing import (
    ZERO_GREEKS,
    Greeks,
    PricingModel,
    default_model_for,
    inputs_for,
)

#: Equity and index scenario ranges of the risk-based estimate.
EQUITY_MOVES = tuple(m / 100 for m in range(-15, 16, 3))
INDEX_MOVES = (-0.08, -0.06, -0.04, -0.02, 0.0, 0.02, 0.04, 0.06)
#: Minimum per short contract (per share) of the risk-based estimate.
MIN_PER_SHORT_CONTRACT = 0.375


@dataclass(frozen=True)
class PortfolioGreeks:
    delta_shares: float = 0.0
    dollar_delta: float = 0.0
    dollar_gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0

    def __add__(self, other: PortfolioGreeks) -> PortfolioGreeks:
        return PortfolioGreeks(
            delta_shares=self.delta_shares + other.delta_shares,
            dollar_delta=self.dollar_delta + other.dollar_delta,
            dollar_gamma=self.dollar_gamma + other.dollar_gamma,
            vega=self.vega + other.vega,
            theta=self.theta + other.theta,
        )


@dataclass(frozen=True)
class OptionRiskView:
    """What the option rules need to know about the market on one day.

    ``greeks`` and ``marks`` are per share of the underlying for each known
    contract; ``spots`` are underlying prices. A contract missing from
    ``greeks`` has unknown Greeks (no quote and no vol to price it)."""

    as_of: date
    contracts: Mapping[str, OptionContract] = field(default_factory=dict[str, OptionContract])
    greeks: Mapping[str, Greeks] = field(default_factory=dict[str, Greeks])
    marks: Mapping[str, float] = field(default_factory=dict[str, float])
    ivs: Mapping[str, float] = field(default_factory=dict[str, float])
    spots: Mapping[str, float] = field(default_factory=dict[str, float])
    rate: float = 0.0
    #: The book's position groups (instrument -> signed quantity each).
    groups: tuple[Mapping[str, float], ...] = ()

    def contract(self, instrument: str) -> OptionContract | None:
        known = self.contracts.get(instrument)
        if known is not None:
            return known
        return parse_contract_id(instrument) if is_option_id(instrument) else None

    def spot_of(self, contract: OptionContract) -> float | None:
        return self.spots.get(contract.underlying)


def position_greeks(
    instrument: str, quantity: float, view: OptionRiskView
) -> PortfolioGreeks | None:
    """Greeks of one position in money terms; ``None`` when unknown."""
    contract = view.contract(instrument)
    if contract is None:
        spot = view.spots.get(instrument)
        if spot is None:
            return None
        return PortfolioGreeks(delta_shares=quantity, dollar_delta=quantity * spot)
    greeks = view.greeks.get(contract.contract_id)
    spot = view.spot_of(contract)
    if greeks is None or spot is None:
        return None
    g = greeks.scaled(quantity * contract.multiplier)
    return PortfolioGreeks(
        delta_shares=g.delta,
        dollar_delta=g.delta * spot,
        dollar_gamma=g.gamma * spot * spot / 100.0,
        vega=g.vega_per_point,
        theta=g.theta_per_day,
    )


def portfolio_greeks(
    positions: Mapping[str, float], view: OptionRiskView
) -> tuple[PortfolioGreeks, list[str]]:
    """The book's Greeks and the instruments whose Greeks are unknown."""
    total = PortfolioGreeks()
    unknown: list[str] = []
    for instrument, qty in positions.items():
        if abs(qty) < 1e-12:
            continue
        g = position_greeks(instrument, qty, view)
        if g is None:
            unknown.append(instrument)
        else:
            total = total + g
    return total, sorted(unknown)


# ---- payoff and max loss --------------------------------------------------------


def _legs(
    positions: Mapping[str, float], view: OptionRiskView
) -> tuple[list[tuple[OptionContract, float]], dict[str, float]]:
    options: list[tuple[OptionContract, float]] = []
    shares: dict[str, float] = {}
    for instrument, qty in positions.items():
        contract = view.contract(instrument)
        if contract is None:
            shares[instrument] = shares.get(instrument, 0.0) + qty
        else:
            options.append((contract, qty))
    return options, shares


def expiry_value(positions: Mapping[str, float], view: OptionRiskView, spot: float) -> float:
    """What the legs are worth at expiry with the underlying at ``spot``
    (one underlying: shares of any ticker are valued at ``spot``)."""
    options, shares = _legs(positions, view)
    value = sum(q * spot for q in shares.values())
    return value + sum(q * c.multiplier * c.intrinsic(spot) for c, q in options)


def worst_expiry_value(positions: Mapping[str, float], view: OptionRiskView) -> float:
    """The minimum of :func:`expiry_value` over every underlying price, or
    ``-inf`` when it falls without bound."""
    options, shares = _legs(positions, view)
    slope_up = sum(shares.values()) + sum(q * c.multiplier for c, q in options if c.right == "call")
    if slope_up < -1e-9:
        return -math.inf
    points = [0.0] + sorted({c.strike for c, _ in options})
    return min(expiry_value(positions, view, s) for s in points)


def current_value(positions: Mapping[str, float], view: OptionRiskView) -> float | None:
    """The legs marked now (``None`` when a mark or spot is missing)."""
    total = 0.0
    for instrument, qty in positions.items():
        contract = view.contract(instrument)
        if contract is None:
            spot = view.spots.get(instrument)
            if spot is None:
                return None
            total += qty * spot
        else:
            mark = view.marks.get(contract.contract_id)
            if mark is None:
                return None
            total += qty * mark * contract.multiplier
    return total


def max_loss(
    positions: Mapping[str, float], view: OptionRiskView, *, cost: float | None = None
) -> float:
    """The most the legs can lose from ``cost`` (what they cost to open;
    default: their current value). ``inf`` for unbounded loss."""
    worst = worst_expiry_value(positions, view)
    if math.isinf(worst):
        return math.inf
    basis = cost if cost is not None else current_value(positions, view)
    if basis is None:
        return math.inf
    return max(basis - worst, 0.0)


def is_unbounded(positions: Mapping[str, float], view: OptionRiskView) -> bool:
    return math.isinf(worst_expiry_value(positions, view))


# ---- margin ---------------------------------------------------------------------


def naked_requirement(contract: OptionContract, quantity: float, spot: float, mark: float) -> float:
    """Reg T requirement of ``quantity`` naked short contracts (a positive
    number of contracts sold)."""
    index = contract.settlement == "cash"
    base_pct = 0.15 if index else 0.20
    otm = max(contract.strike - spot, 0.0) if contract.is_call else max(spot - contract.strike, 0.0)
    floor = 0.10 * (spot if contract.is_call else contract.strike)
    per_share = mark + max(base_pct * spot - otm, floor)
    return per_share * contract.multiplier * abs(quantity)


def reg_t_requirement(positions: Mapping[str, float], view: OptionRiskView) -> float:
    """Strategy-based requirement of one group of legs (see the module doc).

    Short calls are covered by the group's shares and long calls first,
    lowest strike first (the pairing that needs the least); only what is
    left uncovered pays the naked formula."""
    worst = worst_expiry_value(positions, view)
    if not math.isinf(worst):
        return max(-worst, 0.0)
    options, shares = _legs(positions, view)
    cover = sum(shares.values()) + sum(q * c.multiplier for c, q in options if c.is_call and q > 0)
    bounded = dict(positions)
    naked = 0.0
    for contract, qty in sorted(
        ((c, q) for c, q in options if c.is_call and q < 0), key=lambda cq: cq[0].strike
    ):
        need = -qty * contract.multiplier
        covered = min(max(cover, 0.0), need)
        cover -= covered
        uncovered = (need - covered) / contract.multiplier
        if uncovered <= 1e-12:
            continue
        spot = view.spot_of(contract)
        if spot is None:
            return math.inf
        mark = view.marks.get(contract.contract_id, 0.0)
        naked += naked_requirement(contract, uncovered, spot, mark)
        bounded[contract.contract_id] = qty + uncovered
    rest = worst_expiry_value(bounded, view)
    return max(-rest, 0.0) + naked


def _reprice(
    positions: Mapping[str, float],
    view: OptionRiskView,
    move: float,
    model: PricingModel | None,
) -> float | None:
    total = 0.0
    for instrument, qty in positions.items():
        contract = view.contract(instrument)
        if contract is None:
            spot = view.spots.get(instrument)
            if spot is None:
                return None
            total += qty * spot * (1 + move)
            continue
        spot = view.spot_of(contract)
        iv = view.ivs.get(contract.contract_id)
        if spot is None:
            return None
        shocked = spot * (1 + move)
        if iv is None or contract.year_fraction(view.as_of) <= 0:
            price = contract.intrinsic(shocked)
        else:
            pricer = model or default_model_for(contract)
            price = pricer.price(
                inputs_for(contract, view.as_of, spot=shocked, vol=iv, rate=view.rate)
            )
        total += qty * price * contract.multiplier
    return total


def risk_based_requirement(
    positions: Mapping[str, float],
    view: OptionRiskView,
    *,
    model: PricingModel | None = None,
) -> float:
    """Worst loss over the scenario moves (see the module doc)."""
    now = _reprice(positions, view, 0.0, model)
    if now is None:
        return math.inf
    options, _ = _legs(positions, view)
    index = any(c.settlement == "cash" for c, _ in options)
    moves = INDEX_MOVES if index else EQUITY_MOVES
    worst = 0.0
    for move in moves:
        value = _reprice(positions, view, move, model)
        if value is None:
            return math.inf
        worst = max(worst, now - value)
    floor = sum(MIN_PER_SHORT_CONTRACT * c.multiplier * -q for c, q in options if q < 0)
    return max(worst, floor)


def group_positions(
    positions: Mapping[str, float], groups: Iterable[Mapping[str, float]]
) -> list[dict[str, float]]:
    """Split a book into the given groups plus one group per remaining
    position (quantities a group claims are taken from the book first)."""
    left = {k: v for k, v in positions.items() if abs(v) > 1e-12}
    out: list[dict[str, float]] = []
    for group in groups:
        claimed: dict[str, float] = {}
        for instrument, qty in group.items():
            held = left.get(instrument, 0.0)
            if held * qty <= 0:
                continue
            take = qty if abs(held) >= abs(qty) else held
            claimed[instrument] = take
            left[instrument] = held - take
        if claimed:
            out.append(claimed)
    out.extend({k: v} for k, v in left.items() if abs(v) > 1e-12)
    return out


def book_requirement(
    positions: Mapping[str, float],
    view: OptionRiskView,
    groups: Iterable[Mapping[str, float]] = (),
    *,
    method: str = "reg_t",
) -> float:
    """Margin requirement of a book: the sum over its groups. Shares alone
    need nothing here (they are paid in cash)."""
    total = 0.0
    for group in group_positions(positions, groups):
        if not any(view.contract(k) is not None for k in group):
            continue
        if method == "risk_based":
            total += risk_based_requirement(group, view)
        else:
            total += reg_t_requirement(group, view)
    return total


ZERO = PortfolioGreeks()
__all__ = [
    "ZERO",
    "ZERO_GREEKS",
    "OptionRiskView",
    "PortfolioGreeks",
    "book_requirement",
    "current_value",
    "expiry_value",
    "group_positions",
    "is_unbounded",
    "max_loss",
    "naked_requirement",
    "portfolio_greeks",
    "position_greeks",
    "reg_t_requirement",
    "risk_based_requirement",
    "worst_expiry_value",
]
