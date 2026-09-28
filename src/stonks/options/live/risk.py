"""The option risk rules on a live book (roadmap 17.8).

Live option orders run the four registered option rules
(``option_greek_limits``, ``option_max_loss``, ``option_margin``,
``short_option_guard``) against the account as the broker reports it and
the day's live quotes:

- :func:`live_view` builds the rules' :class:`OptionRiskView` from live
  quotes with the broker's Greeks (per share; theta per day and vega per
  vol point, turned into the pricing units here);
- :func:`live_policy` tightens the book's policy for live options. The
  ``short_option_guard`` is always on, capped at the portfolio's approval
  level. ``option_margin`` is always on. ``option_max_loss`` takes the
  tighter of its own limits and ``[production.options]``'s, so every live
  option group has a defined max loss. Greek limits follow the policy;
- :func:`check_combos` runs the rules and keeps or drops each combo as
  one unit. Closing combos are never dropped (P28).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from stonks.core.combos import ComboOrder
from stonks.core.types import Order, Portfolio
from stonks.options.chain import OptionQuote
from stonks.options.live.approval import ApprovalLevel, guard_level
from stonks.options.live.settings import OptionsLiveSettings
from stonks.options.pricing.base import Greeks
from stonks.options.risk import OptionRiskView
from stonks.production.rules import RiskAdjustment, RiskContext, registered_rules
from stonks.production.rules._options import OptionRiskRule


def _greeks(q: OptionQuote) -> Greeks | None:
    if q.delta is None or q.gamma is None or q.vega is None or q.theta is None:
        return None
    return Greeks(
        delta=q.delta,
        gamma=q.gamma,
        vega=q.vega * 100.0,
        theta=q.theta * 365.0,
        rho=(q.rho or 0.0) * 100.0,
    )


def live_view(
    as_of: date,
    quotes: Mapping[str, OptionQuote],
    spots: Mapping[str, float],
    *,
    rate: float = 0.0,
) -> OptionRiskView:
    """The rules' market from live quotes. A quote's underlying price fills
    a spot the caller does not give."""
    contracts = {cid: q.contract for cid, q in quotes.items()}
    greeks = {cid: g for cid, q in quotes.items() if (g := _greeks(q)) is not None}
    marks = {cid: m for cid, q in quotes.items() if (m := q.mark) is not None}
    ivs = {cid: q.iv for cid, q in quotes.items() if q.iv is not None}
    all_spots = dict(spots)
    for q in quotes.values():
        if q.underlying_price and q.contract.underlying not in all_spots:
            all_spots[q.contract.underlying] = q.underlying_price
    return OptionRiskView(
        as_of=as_of,
        contracts=contracts,
        greeks=greeks,
        marks=marks,
        ivs=ivs,
        spots=all_spots,
        rate=rate,
    )


def live_policy(policy: Any, level: ApprovalLevel, settings: OptionsLiveSettings) -> Any:
    """``policy`` tightened for live options at ``level`` (see the module doc)."""
    rules = policy.rules
    guard = rules.short_option_guard
    allowed = guard_level(level) or 1
    cap = min(guard.approval_level, allowed) if guard.enabled else allowed
    max_loss = rules.option_max_loss

    def tighter(own: float | None, live: float) -> float:
        return live if own is None else min(own, live)

    new_rules = rules.model_copy(
        update={
            "short_option_guard": guard.model_copy(update={"enabled": True, "approval_level": cap}),
            "option_margin": rules.option_margin.model_copy(update={"enabled": True}),
            "option_max_loss": max_loss.model_copy(
                update={
                    "max_loss_per_group": tighter(
                        max_loss.max_loss_per_group, settings.max_loss_per_group
                    ),
                    "max_loss_total": tighter(max_loss.max_loss_total, settings.max_loss_total),
                }
            ),
        }
    )
    return policy.model_copy(update={"rules": new_rules})


@dataclass(frozen=True)
class RiskOutcome:
    kept: tuple[ComboOrder, ...] = ()
    #: Combo client id -> why it was dropped.
    dropped: Mapping[str, str] = field(default_factory=dict[str, str])
    adjustments: tuple[RiskAdjustment, ...] = ()


def leg_orders(combo: ComboOrder, held: Mapping[str, float]) -> list[Order]:
    """The combo's legs as orders, each marked ``close`` when it shrinks a
    held position without crossing zero, else ``open``."""
    out: list[Order] = []
    for order in combo.leg_orders():
        have = held.get(order.ticker, 0.0)
        qty = order.quantity if order.side == "buy" else -order.quantity
        closes = have * qty < 0 and abs(qty) <= abs(have) + 1e-9
        out.append(
            Order(
                client_id=order.client_id,
                ticker=order.ticker,
                side=order.side,
                quantity=order.quantity,
                strategy_id=order.strategy_id,
                position_effect="close" if closes else "open",
                decision_context=order.decision_context,
            )
        )
    return out


def option_rules() -> list[OptionRiskRule]:
    return [r for r in registered_rules() if isinstance(r, OptionRiskRule)]


def check_combos(
    combos: Sequence[ComboOrder],
    *,
    portfolio: Portfolio,
    prices: Mapping[str, float],
    view: OptionRiskView,
    policy: Any,
    as_of: date,
    portfolio_id: str | None = None,
) -> RiskOutcome:
    """Run the option rules over ``combos``. ``prices`` are per contract for
    options (mark times multiplier) and per share for stocks, so the book's
    value is its equity."""
    orders: list[Order] = []
    for combo in combos:
        legs = leg_orders(combo, portfolio.positions)
        mixed = {o.position_effect for o in legs} == {"close", "open"}
        for o in legs:
            if mixed and o.position_effect == "close":
                # a roll: its closing leg is its own unit, taken first like
                # every exit, so the opening legs are judged on the book
                # after the close (a rolled cash-secured put stays level 2)
                o = replace(
                    o,
                    decision_context={
                        **(o.decision_context or {}),
                        "combo_id": f"{combo.client_id}:close",
                    },
                )
            orders.append(o)
    ctx = RiskContext(
        portfolio=portfolio,
        prices=prices,
        asset_classes={},
        policy=policy,
        as_of=as_of,
        portfolio_id=portfolio_id,
        options=view,
    )
    adjustments: list[RiskAdjustment] = []
    for rule in option_rules():
        orders, adj = rule.apply(orders, ctx)
        adjustments.extend(adj)
    kept_ids = {o.client_id for o in orders}
    kept: list[ComboOrder] = []
    dropped: dict[str, str] = {}
    for combo in combos:
        legs = [f"{combo.client_id}:{i}" for i in range(len(combo.legs))]
        if all(leg in kept_ids for leg in legs):
            kept.append(combo)
            continue
        reasons = [
            a.reason for a in adjustments if a.ticker in {leg.instrument for leg in combo.legs}
        ]
        dropped[combo.client_id] = reasons[0] if reasons else "dropped by the option rules"
    return RiskOutcome(kept=tuple(kept), dropped=dropped, adjustments=tuple(adjustments))


def contract_prices(
    quotes: Mapping[str, OptionQuote], spots: Mapping[str, float]
) -> dict[str, float]:
    """Per-contract option marks and per-share stock spots, the prices the
    rules value the book at."""
    prices = {u: float(p) for u, p in spots.items()}
    for cid, q in quotes.items():
        mark = q.mark
        if mark is not None:
            prices[cid] = mark * q.contract.multiplier
    return prices
