"""The market a contract is priced in (roadmap 17.7).

:class:`PricingMarket` pairs a :class:`~stonks.options.rates.RateCurve`
with a :class:`~stonks.options.dividends.DividendForecast` and builds the
:class:`~stonks.options.pricing.PricingInputs` of a contract on a pricing
day: the zero rate at its expiry and the dividend yield until its expiry.
Chains, Greeks, implied vol, risk and the options backtest all take one,
so no caller passes a flat rate by hand. ``PricingMarket.flat`` is the old
behaviour (one rate, one yield).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from stonks.core.options import OptionContract
from stonks.options.dividends import DividendForecast, FlatDividendYield, NoDividends
from stonks.options.pricing import PricingInputs
from stonks.options.rates import FlatRateCurve, RateCurve


@dataclass(frozen=True)
class PricingMarket:
    rates: RateCurve = field(default_factory=FlatRateCurve)
    dividends: DividendForecast = field(default_factory=NoDividends)

    @classmethod
    def flat(cls, rate: float = 0.0, dividend_yield: float = 0.0) -> PricingMarket:
        dividends: DividendForecast = (
            FlatDividendYield(dividend_yield) if dividend_yield else NoDividends()
        )
        return cls(FlatRateCurve(rate), dividends)

    def rate(self, contract: OptionContract, as_of: date) -> float:
        """The zero rate from ``as_of`` to the contract's expiry."""
        return self.rates.zero_rate(as_of, contract.year_fraction(as_of))

    def inputs(
        self, contract: OptionContract, as_of: date, *, spot: float, vol: float
    ) -> PricingInputs:
        time = contract.year_fraction(as_of)
        rate = self.rates.zero_rate(as_of, time)
        q = (
            self.dividends.dividend_yield(
                contract.underlying, as_of, contract.expiry, spot, self.rates
            )
            if time > 0
            else 0.0
        )
        return PricingInputs(
            right=contract.right,
            spot=spot,
            strike=contract.strike,
            time=time,
            rate=rate,
            dividend_yield=q,
            vol=vol,
        )


def market_or_flat(
    market: PricingMarket | None, rate: float = 0.0, dividend_yield: float = 0.0
) -> PricingMarket:
    """``market`` when given, else a flat market of ``rate`` and ``dividend_yield``."""
    return market if market is not None else PricingMarket.flat(rate, dividend_yield)
