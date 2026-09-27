"""Helpers shared by the options strategies: holding shares, valuing a
position group from the day's chain, and the usual exit checks."""

from __future__ import annotations

import math
from datetime import date

from stonks.backtest.options_ledger import PositionGroup
from stonks.core.params import ParameterSpec
from stonks.options.analytics import analyze
from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.strategy import OptionDecisionContext, OptionIntent


def exit_specs(profit_take: float = 0.5, roll_dte: int = 21) -> list[ParameterSpec]:
    return [
        ParameterSpec(
            "profit_take",
            "float",
            profit_take,
            (0.1, 1.0),
            description="close at this share of the maximum profit",
        ),
        ParameterSpec(
            "roll_dte", "int", roll_dte, (0, 60), description="close when this few days are left"
        ),
    ]


def buy_shares(
    ctx: OptionDecisionContext, underlying: str, allocation: float, strategy_id: str
) -> ComboOrder | None:
    """A round-lot share purchase up to ``allocation`` of equity, or
    ``None`` when the book already holds shares or cannot afford 100."""
    spot = ctx.spot(underlying)
    if spot is None or ctx.shares(underlying) > 0:
        return None
    lots = math.floor(min(allocation * ctx.equity, ctx.ledger.cash) / (spot * 100.0) + 1e-9)
    if lots < 1:
        return None
    return ComboOrder(
        client_id=f"{strategy_id}:{ctx.as_of.isoformat()}:{underlying}:shares",
        legs=(ComboLeg("buy", 100.0 * lots, shares=underlying),),
        structure="shares",
        group_id=f"shares:{underlying}",
        strategy_id=strategy_id,
        decided_at=ctx.as_of,
    )


def group_value(ctx: OptionDecisionContext, group: PositionGroup) -> float | None:
    """The group's option legs at today's marks (``None`` if one is missing)."""
    total = 0.0
    for instrument, qty in group.legs.items():
        contract = ctx.ledger.contracts.get(instrument)
        if contract is None:
            continue
        chain = ctx.chains.get(contract.underlying)
        quote = chain.by_id().get(instrument) if chain is not None else None
        mark = quote.mark if quote is not None else None
        if mark is None:
            return None
        total += qty * mark * contract.multiplier
    return total


def option_open_cost(group: PositionGroup) -> float:
    return group.open_cost


def days_left(ctx: OptionDecisionContext, group: PositionGroup) -> int | None:
    expiries = [ctx.ledger.contracts[k].expiry for k in group.legs if k in ctx.ledger.contracts]
    return min((e - ctx.as_of).days for e in expiries) if expiries else None


def profit_fraction(ctx: OptionDecisionContext, group: PositionGroup) -> float | None:
    """Profit so far over the most the group can make: the credit for a
    credit structure, the debit paid for a debit one (a doubling counts 1)."""
    value = group_value(ctx, group)
    if value is None or abs(group.open_cost) < 1e-9:
        return None
    pnl = value - group.open_cost
    return pnl / abs(group.open_cost)


def exit_intent(
    ctx: OptionDecisionContext, group: PositionGroup, profit_take: float, roll_dte: int
) -> OptionIntent | None:
    """A close when the group hit its profit target or is near expiry."""
    left = days_left(ctx, group)
    if left is None:
        return None
    frac = profit_fraction(ctx, group)
    if frac is not None and frac >= profit_take:
        return _close(group, ctx.as_of, f"profit {frac:.0%}")
    if left <= roll_dte:
        return _close(group, ctx.as_of, f"{left} days left")
    return None


def _close(group: PositionGroup, as_of: date, reason: str) -> OptionIntent:
    underlying = next(iter(group.legs)).split(":")[0]
    return OptionIntent(underlying, "close_group", group_id=group.group_id, reason=reason)


def option_groups(ctx: OptionDecisionContext, underlying: str, structures: tuple[str, ...]):
    return [
        g
        for g in ctx.groups(underlying=underlying)
        if g.structure in structures and any(k in ctx.ledger.contracts for k in g.legs)
    ]


def atm_iv(ctx: OptionDecisionContext, underlying: str, target_dte: int = 30) -> float | None:
    """The implied vol of the call nearest the money at the expiry nearest
    ``target_dte`` (the vendor's vol or the vol its mark implies)."""
    chain = ctx.chains.get(underlying)
    spot = ctx.spot(underlying)
    if chain is None or spot is None or not len(chain):
        return None
    expiries = [e for e in chain.expiries() if (e - ctx.as_of).days >= 7]
    if not expiries:
        return None
    expiry = min(expiries, key=lambda e: abs((e - ctx.as_of).days - target_dte))
    calls = [q for q in chain.filter(right="call", expiry=expiry) if q.two_sided]
    if not calls:
        return None
    quote = min(calls, key=lambda q: abs(q.contract.strike - spot))
    return analyze(quote, spot, rate=ctx.rate).iv


def sma(values: list[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n
