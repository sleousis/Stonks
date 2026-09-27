"""The reference price of a ticker for a live book (roadmap 19.6).

A live, non-delayed quote wins (its last trade, else its mid). Then a
delayed quote, then the lake close in ``ctx.prices``. The source travels
with the price so a band never treats a delayed quote or a close as live:
it tightens instead (``price_band.delayed_band_pct``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from stonks.production.rules import RiskContext

ReferenceSource = Literal["live", "delayed", "close"]


@dataclass(frozen=True)
class Reference:
    price: float
    source: ReferenceSource
    #: The live quote's bid and ask (``None`` for a delayed quote or a close).
    bid: float | None = None
    ask: float | None = None

    @property
    def is_live(self) -> bool:
        return self.source == "live"


def _positive(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def reference_price(ctx: RiskContext, ticker: str) -> Reference | None:
    """``ticker``'s reference for ``ctx``, or ``None`` with no price at all."""
    quote = ctx.live.quotes.get(ticker) if ctx.live is not None else None
    if quote is not None:
        ref = _positive(quote.reference)
        if ref is not None:
            if quote.delayed:
                return Reference(price=ref, source="delayed")
            return Reference(
                price=ref, source="live", bid=_positive(quote.bid), ask=_positive(quote.ask)
            )
    close = _positive(ctx.prices.get(ticker))
    return Reference(price=close, source="close") if close is not None else None
