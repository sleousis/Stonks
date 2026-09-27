"""The pre-open gap check of the submit window (roadmap 19.14,
``docs/design/live-trading.md`` section 4).

A live book decides after the close and its tickets go out before the next
open. The price may move overnight. Just before sending, the submit window
reads the latest pre-open quote of every opening order and holds the order
when the price moved beyond the band since the decision:

- the limit is ``price_band.max_gap_pct``, else ``price_band.band_pct``.
  With the band off the check is off;
- an opening order with no quote is held (its move is unknown);
- an order with no decision price is not measured (the band at the tick
  already collared it);
- a closing order is never held (P28).

A delayed quote counts: before the open it is the best view there is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from stonks.core.types import Order
from stonks.execution.brokers.base import Quote
from stonks.production.rules.price_band import PriceBandSettings

__all__ = ["gap_limit", "is_opening", "preopen_holds"]


def gap_limit(band: PriceBandSettings | None) -> float | None:
    """The largest move since the decision an opening order may take, or
    ``None`` when the band is off."""
    if band is None or not band.active:
        return None
    return band.max_gap_pct if band.max_gap_pct is not None else band.band_pct


def is_opening(order: Order) -> bool:
    """A buy, or a short sale. A sell of a long and a cover are closes."""
    if order.position_effect is not None:
        return order.position_effect == "open"
    return order.side == "buy"


def preopen_holds(
    orders: Sequence[Order], quotes: Mapping[str, Quote], limit: float | None
) -> dict[str, str]:
    """The client ids to hold, each with its reason."""
    if limit is None:
        return {}
    holds: dict[str, str] = {}
    for order in orders:
        if not is_opening(order):
            continue
        decided = order.decision_price
        if decided is None or not decided > 0:
            continue
        quote = quotes.get(order.ticker)
        now = quote.reference if quote is not None else None
        if now is None or not now > 0:
            holds[order.client_id] = "no pre-open quote, so the move since the decision is unknown"
            continue
        gap = abs(now / decided - 1.0)
        if gap > limit:
            holds[order.client_id] = f"price moved {gap:.2%} since the decision (limit {limit:.2%})"
    return holds
