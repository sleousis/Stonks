"""Limits at the mid for live option orders (roadmap 17.8).

Option orders are never market orders. Each leg is priced from its live
quote: the mid, moved toward the far side by ``collar_share`` of the half
spread (0 keeps it at the mid, 1 puts it at the touch). A combo's net
limit is the sum of its legs' limits times their ratios, positive for a
debit and negative for a credit, the way the broker quotes it.

A leg with no two-sided quote, or a spread wider than ``max_spread_pct``
of its mid, is not priced: the combo is not made and the caller says why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from stonks.core.combos import ComboOrder
from stonks.options.chain import OptionQuote


@dataclass(frozen=True)
class Priced:
    """A combo with its net limit, or why it could not be priced."""

    combo: ComboOrder | None
    reason: str | None = None
    #: The limit per share of each leg, by instrument.
    leg_limits: Mapping[str, float] | None = None


def leg_limit(
    quote: OptionQuote | None, side: str, *, collar_share: float, max_spread_pct: float
) -> tuple[float | None, str | None]:
    """The limit of one leg, or ``None`` and the reason."""
    if quote is None:
        return None, "no live quote"
    mid, half = quote.mid, quote.half_spread
    if mid is None or half is None or mid <= 0:
        return None, f"no two-sided quote for {quote.contract_id}"
    spread = quote.spread_pct
    if spread is not None and spread > max_spread_pct:
        return None, (
            f"{quote.contract_id} spread is {spread:.0%} of the mid, over {max_spread_pct:.0%}"
        )
    step = collar_share * half
    return (mid + step if side == "buy" else mid - step), None


def price_combo(
    combo: ComboOrder,
    quotes: Mapping[str, OptionQuote],
    *,
    collar_share: float,
    max_spread_pct: float,
) -> Priced:
    """``combo`` with its net limit at the mids. Stock legs are not priced
    here: a combo with one is refused (buy-writes go out as two orders)."""
    net = 0.0
    limits: dict[str, float] = {}
    for leg in combo.legs:
        if leg.contract is None:
            return Priced(None, f"{combo.client_id}: a stock leg is not priced at the mid")
        price, why = leg_limit(
            quotes.get(leg.instrument),
            leg.side,
            collar_share=collar_share,
            max_spread_pct=max_spread_pct,
        )
        if price is None:
            return Priced(None, why)
        limits[leg.instrument] = price
        net += (1.0 if leg.side == "buy" else -1.0) * leg.ratio * price
    return Priced(replace(combo, net_limit=round(net, 6)), leg_limits=limits)
