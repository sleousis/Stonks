"""A synthetic options data source for tests and demos (roadmap 17.1).

:class:`SyntheticOptionSource` is the FakeDataSource path for options: it
builds end-of-day chains from underlying closes with a pricing model, a
fixed implied vol (optionally skewed) and a bid-ask spread. Monthly
expiries fall on the third Friday.

Synthetic quotes are never evidence for a strategy: they carry the model's
assumptions by construction. They exist so the chain ingest, the options
backtest and the risk rules can be tested without a vendor.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from stonks.core.options import ExerciseStyle, OptionRight, Settlement
from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sources.base import DataSource, UnsupportedCapabilityError
from stonks.options.pricing import PricingInputs, pricing_model


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def monthly_expiries(as_of: date, horizon_days: int) -> list[date]:
    out: list[date] = []
    year, month = as_of.year, as_of.month
    while True:
        expiry = third_friday(year, month)
        if (expiry - as_of).days > horizon_days:
            return out
        if expiry >= as_of:
            out.append(expiry)
        month += 1
        if month > 12:
            year, month = year + 1, 1


@dataclass(frozen=True)
class SyntheticChainSpec:
    vol: float = 0.25
    #: Vol added per unit of log-moneyness below the forward (a put skew).
    skew: float = 0.0
    rate: float = 0.0
    dividend_yield: float = 0.0
    #: Half spread as a share of the model price, and its floor in dollars.
    half_spread_pct: float = 0.02
    min_half_spread: float = 0.01
    #: Strike grid: ``strikes_each_side`` strikes above and below spot,
    #: ``strike_step_pct`` of spot apart (rounded to a cent).
    strikes_each_side: int = 6
    strike_step_pct: float = 0.05
    horizon_days: int = 100
    multiplier: float = 100.0
    style: ExerciseStyle = "american"
    settlement: Settlement = "physical"
    model: str = "black_scholes"
    #: Quotes cheaper than this are sent without a bid, as real chains do.
    min_bid: float = 0.05


class SyntheticOptionSource(DataSource):
    source_id = "synthetic"

    def __init__(
        self,
        closes: Mapping[str, Mapping[date, float]],
        spec: SyntheticChainSpec | None = None,
        *,
        fixed_strikes: Mapping[str, Iterable[float]] | None = None,
    ) -> None:
        self._closes = {t: dict(v) for t, v in closes.items()}
        self._spec = spec or SyntheticChainSpec()
        self._model = pricing_model(self._spec.model)
        # A fixed strike grid per underlying keeps the same contracts listed
        # from day to day (real chains do not re-centre every day).
        self._fixed = {t: sorted(set(ks)) for t, ks in (fixed_strikes or {}).items()}

    # ---- the DataSource surface this source does not serve ------------------

    def list_tickers(self, exchange: str) -> list[str]:
        return sorted(self._closes)

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        raise UnsupportedCapabilityError("the synthetic source serves option quotes only")

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise UnsupportedCapabilityError("the synthetic source serves option quotes only")

    # ---- options -------------------------------------------------------------

    def strikes(self, underlying: str, spot: float) -> list[float]:
        if underlying in self._fixed:
            return self._fixed[underlying]
        s = self._spec
        step = max(round(spot * s.strike_step_pct, 2), 0.01)
        centre = round(spot / step) * step
        return [
            round(centre + i * step, 2)
            for i in range(-s.strikes_each_side, s.strikes_each_side + 1)
            if centre + i * step > 0
        ]

    def quote(
        self,
        underlying: str,
        as_of: date,
        spot: float,
        expiry: date,
        strike: float,
        right: OptionRight,
    ) -> OptionQuoteRow:
        s = self._spec
        time = max((expiry - as_of).days, 0) / 365.0
        forward = spot * math.exp((s.rate - s.dividend_yield) * time)
        vol = max(s.vol + s.skew * max(-math.log(strike / forward), 0.0), 0.01)
        inputs = PricingInputs(right, spot, strike, time, s.rate, s.dividend_yield, vol)
        price = self._model.price(inputs)
        greeks = self._model.greeks(inputs)
        half = max(price * s.half_spread_pct, s.min_half_spread)
        bid = round(price - half, 2)
        ask = round(price + half, 2)
        return OptionQuoteRow(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
            multiplier=s.multiplier,
            style=s.style,
            settlement=s.settlement,
            as_of=as_of,
            bid=bid if bid >= s.min_bid else None,
            ask=max(ask, 0.01),
            last=round(price, 2) if price >= 0.01 else None,
            volume=100.0,
            open_interest=1000.0,
            underlying_price=spot,
            iv=vol if time > 0 else None,
            delta=greeks.delta,
            gamma=greeks.gamma,
            theta=greeks.theta_per_day,
            vega=greeks.vega_per_point,
            rho=greeks.rho / 100.0,
        )

    def fetch_option_quotes(
        self, underlying: str, since: date | None = None, until: date | None = None
    ) -> Iterable[OptionQuoteRow]:
        closes = self._closes.get(underlying)
        if not closes:
            raise UnsupportedCapabilityError(f"no synthetic closes for {underlying!r}")
        rows: list[OptionQuoteRow] = []
        for day in sorted(closes):
            if (since is not None and day < since) or (until is not None and day > until):
                continue
            spot = closes[day]
            for expiry in monthly_expiries(day, self._spec.horizon_days):
                for strike in self.strikes(underlying, spot):
                    for right in ("call", "put"):
                        rows.append(self.quote(underlying, day, spot, expiry, strike, right))
        return rows
