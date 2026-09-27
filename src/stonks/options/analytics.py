"""Our own implied vol and Greeks for a chain (roadmap 17.2).

Vendors send Greeks in different units and some send none, so risk uses
Greeks we compute: the vendor's implied vol when it sent one, else the vol
implied by the quote's mark, then the pricing model's Greeks at that vol.
A quote whose vol cannot be found has no Greeks (never a guess).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from stonks.core.options import OptionContract
from stonks.options.chain import ChainSnapshot, OptionQuote
from stonks.options.pricing import (
    Greeks,
    PricingInputs,
    default_model_for,
    inputs_for,
    pricing_model,
)
from stonks.options.risk import OptionRiskView


@dataclass(frozen=True)
class QuoteAnalytics:
    iv: float | None
    greeks: Greeks | None
    mark: float | None


def analyze(
    quote: OptionQuote,
    spot: float | None,
    *,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    model_name: str | None = None,
) -> QuoteAnalytics:
    mark = quote.mark
    spot = spot or quote.underlying_price
    contract = quote.contract
    if spot is None or contract.year_fraction(quote.as_of) <= 0:
        return QuoteAnalytics(None, None, mark)
    model = pricing_model(model_name) if model_name else default_model_for(contract)
    base = inputs_for(
        contract, quote.as_of, spot=spot, vol=0.2, rate=rate, dividend_yield=dividend_yield
    )
    iv = quote.iv
    if iv is None and mark is not None:
        iv = model.implied_vol(base, mark)
    if iv is None:
        return QuoteAnalytics(None, None, mark)
    return QuoteAnalytics(iv, model.greeks(base.with_vol(iv)), mark)


def model_mark(
    contract: OptionContract,
    as_of: date,
    spot: float,
    iv: float | None,
    *,
    rate: float = 0.0,
) -> float:
    """A fallback mark when the day has no quote: the model at the last
    known vol, or the intrinsic value without one."""
    if iv is None or contract.year_fraction(as_of) <= 0:
        return contract.intrinsic(spot)
    inputs: PricingInputs = inputs_for(contract, as_of, spot=spot, vol=iv, rate=rate)
    return default_model_for(contract).price(inputs)


def risk_view(
    as_of: date,
    chains: Mapping[str, ChainSnapshot],
    spots: Mapping[str, float],
    *,
    contracts: Mapping[str, OptionContract] | None = None,
    marks: Mapping[str, float] | None = None,
    ivs: Mapping[str, float] | None = None,
    groups: Iterable[Mapping[str, float]] = (),
    rate: float = 0.0,
    only: Iterable[str] | None = None,
) -> OptionRiskView:
    """The option risk view of one day: the chain quotes analysed (only the
    contract ids in ``only`` when given, which keeps a backtest fast), plus
    held contracts with no quote priced at the ``marks`` and ``ivs`` given."""
    wanted = None if only is None else set(only)
    all_contracts: dict[str, OptionContract] = dict(contracts or {})
    greeks: dict[str, Greeks] = {}
    out_marks: dict[str, float] = dict(marks or {})
    out_ivs: dict[str, float] = dict(ivs or {})
    for underlying, chain in chains.items():
        spot = spots.get(underlying, chain.spot or 0.0) or None
        for quote in chain:
            cid = quote.contract_id
            if wanted is not None and cid not in wanted:
                continue
            a = analyze(quote, spot, rate=rate)
            all_contracts[cid] = quote.contract
            if a.mark is not None:
                out_marks.setdefault(cid, a.mark)
            if a.iv is not None:
                out_ivs[cid] = a.iv
            if a.greeks is not None:
                greeks[cid] = a.greeks
    for cid, contract in all_contracts.items():
        if cid in greeks:
            continue
        spot = spots.get(contract.underlying)
        iv = out_ivs.get(cid)
        if spot is None or iv is None or contract.year_fraction(as_of) <= 0:
            continue
        greeks[cid] = default_model_for(contract).greeks(
            inputs_for(contract, as_of, spot=spot, vol=iv, rate=rate)
        )
    return OptionRiskView(
        as_of=as_of,
        contracts=all_contracts,
        greeks=greeks,
        marks=out_marks,
        ivs=out_ivs,
        spots=dict(spots),
        rate=rate,
        groups=tuple(dict(g) for g in groups),
    )
