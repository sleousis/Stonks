"""Pick contracts from a chain snapshot (roadmap 17.5, design section 6).

:class:`LegSelector` finds the expiry nearest a target days-to-expiry
(inside ``[min_dte, max_dte]``), then the strike whose delta is nearest a
target (or the strike nearest a target price). Only quotes that pass the
liquidity filters count: a two-sided market, a spread no wider than
``max_spread_pct`` of the mid, and at least ``min_open_interest``.

Delta comes from the quote when the vendor sent one; otherwise from the
pricing model with the quote's implied vol, or with the vol implied by its
mid. A quote whose delta cannot be found is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from stonks.options.chain import ChainSnapshot, OptionQuote
from stonks.options.pricing import default_model_for, inputs_for


@dataclass(frozen=True)
class LegSelector:
    min_dte: int = 7
    max_dte: int = 60
    max_spread_pct: float = 0.5
    min_open_interest: float = 0.0
    rate: float = 0.0

    def liquid(self, quote: OptionQuote) -> bool:
        if not quote.two_sided:
            return False
        spread = quote.spread_pct
        if spread is None or spread > self.max_spread_pct:
            return False
        return (quote.open_interest or 0.0) >= self.min_open_interest

    def expiry(self, chain: ChainSnapshot, target_dte: int) -> date | None:
        """The listed expiry nearest ``target_dte`` inside the DTE window
        (the later one on a tie)."""
        candidates = [
            e for e in chain.expiries() if self.min_dte <= (e - chain.as_of).days <= self.max_dte
        ]
        if not candidates:
            return None
        return min(
            candidates, key=lambda e: (abs((e - chain.as_of).days - target_dte), -e.toordinal())
        )

    def delta(self, quote: OptionQuote, spot: float | None) -> float | None:
        if quote.delta is not None:
            return quote.delta
        spot = spot or quote.underlying_price
        if spot is None:
            return None
        contract = quote.contract
        if contract.year_fraction(quote.as_of) <= 0:
            return None
        model = default_model_for(contract)
        iv = quote.iv
        if iv is None and quote.mid is not None:
            base = inputs_for(contract, quote.as_of, spot=spot, vol=0.2, rate=self.rate)
            iv = model.implied_vol(base, quote.mid)
        if iv is None:
            return None
        return model.greeks(
            inputs_for(contract, quote.as_of, spot=spot, vol=iv, rate=self.rate)
        ).delta

    def by_delta(
        self,
        chain: ChainSnapshot,
        right: str,
        target_delta: float,
        target_dte: int,
        *,
        expiry: date | None = None,
    ) -> OptionQuote | None:
        """The liquid quote whose |delta| is nearest ``|target_delta|``."""
        expiry = expiry or self.expiry(chain, target_dte)
        if expiry is None:
            return None
        best: tuple[float, float, OptionQuote] | None = None
        for q in chain.filter(right=right, expiry=expiry):
            if not self.liquid(q):
                continue
            d = self.delta(q, chain.spot)
            if d is None:
                continue
            key = (abs(abs(d) - abs(target_delta)), q.contract.strike)
            if best is None or key < best[:2]:
                best = (key[0], key[1], q)
        return None if best is None else best[2]

    def by_strike(
        self, chain: ChainSnapshot, right: str, target_strike: float, expiry: date
    ) -> OptionQuote | None:
        """The liquid quote of ``expiry`` whose strike is nearest."""
        quotes = [q for q in chain.filter(right=right, expiry=expiry) if self.liquid(q)]
        if not quotes:
            return None
        return min(
            quotes, key=lambda q: (abs(q.contract.strike - target_strike), q.contract.strike)
        )
