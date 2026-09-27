"""Pricing models that wrap QuantLib (roadmap 17.2).

- ``black_scholes``: European options on a stock paying a continuous
  dividend yield (Black-Scholes-Merton), analytic Greeks and implied vol
  from QuantLib's ``BlackCalculator`` and ``blackFormulaImpliedStdDev``.
- ``black76``: European options on a futures or forward price (Black
  1976). ``spot`` is the futures price and ``dividend_yield`` is ignored
  (the futures carry is already in the price).
- ``american_baw``: the Barone-Adesi-Whaley (1987) quadratic approximation.
- ``american_bjerksund``: the Bjerksund-Stensland (1993) approximation.
- ``american_binomial``: a Cox-Ross-Rubinstein tree (``STEPS`` steps).

QuantLib prices American options between dates, so a model maps
``time`` onto whole days: it rounds the tenor up to ``n`` days and rescales
rate, dividend yield and variance by ``time / (n / 365)``. Under constant
parameters that time change leaves the price unchanged, so a fractional
tenor prices exactly. American Greeks are finite differences of that
price.
"""

from __future__ import annotations

import math
from typing import Any

import QuantLib as ql

from stonks.options.pricing.base import (
    IV_TOLERANCE,
    MAX_VOL,
    MIN_VOL,
    Greeks,
    PricingInputs,
    PricingModel,
    register_pricing_model,
    solve_implied_vol,
)

#: Steps of the binomial tree.
STEPS = 801
_EVALUATION_DATE = ql.Date(2, 1, 2001)


def _option_type(right: str) -> Any:
    return ql.Option.Call if right == "call" else ql.Option.Put


def _black_calculator(inputs: PricingInputs, forward: float) -> Any:
    std_dev = inputs.vol * math.sqrt(inputs.time)
    discount = math.exp(-inputs.rate * inputs.time)
    payoff = ql.PlainVanillaPayoff(_option_type(inputs.right), inputs.strike)
    return ql.BlackCalculator(payoff, forward, std_dev, discount)


class _BlackFamily(PricingModel):
    """Shared by the closed-form European models; subclasses pick the forward."""

    style = "european"

    def forward(self, inputs: PricingInputs) -> float:
        raise NotImplementedError

    def price(self, inputs: PricingInputs) -> float:
        if inputs.time <= 0 or inputs.vol <= 0:
            return self._degenerate_price(inputs)
        return float(_black_calculator(inputs, self.forward(inputs)).value())

    def _degenerate_price(self, inputs: PricingInputs) -> float:
        """No time or no vol: the discounted intrinsic value of the forward."""
        discount = math.exp(-inputs.rate * inputs.time)
        forward = self.forward(inputs)
        if inputs.right == "call":
            return discount * max(forward - inputs.strike, 0.0)
        return discount * max(inputs.strike - forward, 0.0)

    def greeks(self, inputs: PricingInputs) -> Greeks:
        if inputs.time <= 0 or inputs.vol <= 0:
            return super().greeks(inputs)
        calc = _black_calculator(inputs, self.forward(inputs))
        return self._greeks(calc, inputs)

    def _greeks(self, calc: Any, inputs: PricingInputs) -> Greeks:
        raise NotImplementedError

    def implied_vol(self, inputs: PricingInputs, price: float) -> float | None:
        if not math.isfinite(price) or price <= 0 or inputs.time <= 0:
            return None
        forward = self.forward(inputs)
        discount = math.exp(-inputs.rate * inputs.time)
        lower = self._degenerate_price(inputs)
        upper = discount * (forward if inputs.right == "call" else inputs.strike)
        if price < lower - IV_TOLERANCE or price >= upper:
            return None
        try:
            std_dev = ql.blackFormulaImpliedStdDev(
                _option_type(inputs.right),
                inputs.strike,
                forward,
                price,
                discount,
                0.0,
                ql.nullDouble(),
                1e-12,
                200,
            )
        except RuntimeError:  # pragma: no cover - bracketed above; defensive
            return solve_implied_vol(self.price, inputs, price)
        vol = float(std_dev) / math.sqrt(inputs.time)
        return vol if MIN_VOL <= vol <= MAX_VOL else None


@register_pricing_model
class BlackScholes(_BlackFamily):
    name = "black_scholes"

    def forward(self, inputs: PricingInputs) -> float:
        return inputs.spot * math.exp((inputs.rate - inputs.dividend_yield) * inputs.time)

    def _greeks(self, calc: Any, inputs: PricingInputs) -> Greeks:
        return Greeks(
            delta=float(calc.delta(inputs.spot)),
            gamma=float(calc.gamma(inputs.spot)),
            vega=float(calc.vega(inputs.time)),
            theta=float(calc.theta(inputs.spot, inputs.time)),
            rho=float(calc.rho(inputs.time)),
        )


@register_pricing_model
class Black76(_BlackFamily):
    name = "black76"

    def forward(self, inputs: PricingInputs) -> float:
        return inputs.spot

    def _greeks(self, calc: Any, inputs: PricingInputs) -> Greeks:
        value = float(calc.value())
        # With a futures underlying the forward does not move with time or
        # rate: rho is -T x value and theta is r x value minus the decay of
        # the time value (Black 1976).
        decay = float(calc.vega(inputs.time)) * inputs.vol / (2.0 * inputs.time)
        return Greeks(
            delta=float(calc.delta(inputs.spot)),
            gamma=float(calc.gamma(inputs.spot)),
            vega=float(calc.vega(inputs.time)),
            theta=inputs.rate * value - decay,
            rho=-inputs.time * value,
        )


class _QuantLibAmerican(PricingModel):
    style = "american"

    def engine(self, process: Any) -> Any:
        raise NotImplementedError

    def price(self, inputs: PricingInputs) -> float:
        if inputs.time <= 0:
            return inputs.intrinsic
        days = max(math.ceil(inputs.time * 365.0 - 1e-9), 1)
        scale = inputs.time / (days / 365.0)
        vol = max(inputs.vol, 1e-8) * math.sqrt(scale)
        ql.Settings.instance().evaluationDate = _EVALUATION_DATE
        day_count = ql.Actual365Fixed()
        expiry = _EVALUATION_DATE + days
        process = ql.BlackScholesMertonProcess(
            ql.QuoteHandle(ql.SimpleQuote(inputs.spot)),
            ql.YieldTermStructureHandle(
                ql.FlatForward(_EVALUATION_DATE, inputs.dividend_yield * scale, day_count)
            ),
            ql.YieldTermStructureHandle(
                ql.FlatForward(_EVALUATION_DATE, inputs.rate * scale, day_count)
            ),
            ql.BlackVolTermStructureHandle(
                ql.BlackConstantVol(_EVALUATION_DATE, ql.NullCalendar(), vol, day_count)
            ),
        )
        option = ql.VanillaOption(
            ql.PlainVanillaPayoff(_option_type(inputs.right), inputs.strike),
            ql.AmericanExercise(_EVALUATION_DATE, expiry),
        )
        option.setPricingEngine(self.engine(process))
        try:
            value = float(option.NPV())
        except RuntimeError:
            # Near-zero vol breaks the engines (a tree's up-probability
            # leaves [0, 1]). The option is then worth its lower bound: the
            # larger of exercising now and the European value.
            value = BlackScholes().price(inputs)
        if not math.isfinite(value):  # pragma: no cover - defensive
            value = BlackScholes().price(inputs)
        return max(value, inputs.intrinsic)


@register_pricing_model
class AmericanBaroneAdesiWhaley(_QuantLibAmerican):
    name = "american_baw"

    def engine(self, process: Any) -> Any:
        return ql.BaroneAdesiWhaleyApproximationEngine(process)


@register_pricing_model
class AmericanBjerksundStensland(_QuantLibAmerican):
    name = "american_bjerksund"

    def engine(self, process: Any) -> Any:
        return ql.BjerksundStenslandApproximationEngine(process)


@register_pricing_model
class AmericanBinomial(_QuantLibAmerican):
    name = "american_binomial"

    def engine(self, process: Any) -> Any:
        return ql.BinomialVanillaEngine(process, "crr", STEPS)
