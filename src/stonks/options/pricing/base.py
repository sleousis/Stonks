"""The option pricing seam (roadmap 17.2).

A :class:`PricingModel` prices one vanilla option from plain numbers
(:class:`PricingInputs`), returns its Greeks and solves its implied
volatility. Library types (QuantLib) never leave the model modules:
callers pass our inputs and get floats and :class:`Greeks` back.

Models register with ``@register_pricing_model`` and are found by name
(:func:`pricing_model`). Every module in this package is imported on the
first lookup, so a new model is one new file.

Units
-----
- ``time`` is in years (Actual/365 calendar time).
- ``rate`` and ``dividend_yield`` are continuously compounded, per year.
- ``vol`` is a decimal (0.20 is 20%).
- :class:`Greeks` are per share of the underlying: ``vega`` per 1.00 of
  vol (divide by 100 for one vol point, :attr:`Greeks.vega_per_point`),
  ``theta`` per year (:attr:`Greeks.theta_per_day` per calendar day) and
  ``rho`` per 1.00 of rate.

Implied volatility that cannot be solved (a price outside the no-arbitrage
bounds, a stale quote) is ``None``, never a guess.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, ClassVar, Literal

from scipy.optimize import brentq

from stonks.core.options import OptionContract, OptionRight

ModelStyle = Literal["european", "american"]

#: Implied volatility search bracket.
MIN_VOL = 1e-4
MAX_VOL = 5.0
#: Price tolerance of the implied-volatility solve.
IV_TOLERANCE = 1e-8

#: Bumps of the finite-difference Greeks.
_SPOT_BUMP = 0.01  # relative
_VOL_BUMP = 1e-3
_RATE_BUMP = 1e-4
_TIME_BUMP = 1.0 / 365.0


@dataclass(frozen=True)
class PricingInputs:
    """One vanilla option in plain numbers."""

    right: OptionRight
    spot: float
    strike: float
    time: float
    rate: float = 0.0
    dividend_yield: float = 0.0
    vol: float = 0.2

    def __post_init__(self) -> None:
        if self.right not in ("call", "put"):
            raise ValueError(f"right must be call or put, got {self.right!r}")
        for name in ("spot", "strike"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be positive, got {value}")
        if not (math.isfinite(self.time) and self.time >= 0):
            raise ValueError(f"time must be non-negative, got {self.time}")
        if not (math.isfinite(self.vol) and self.vol >= 0):
            raise ValueError(f"vol must be non-negative, got {self.vol}")
        if not (math.isfinite(self.rate) and math.isfinite(self.dividend_yield)):
            raise ValueError("rate and dividend_yield must be finite")

    @property
    def intrinsic(self) -> float:
        if self.right == "call":
            return max(self.spot - self.strike, 0.0)
        return max(self.strike - self.spot, 0.0)

    def with_vol(self, vol: float) -> PricingInputs:
        return replace(self, vol=vol)


@dataclass(frozen=True)
class Greeks:
    """Per share of the underlying; see the module doc for units."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float

    @property
    def vega_per_point(self) -> float:
        return self.vega / 100.0

    @property
    def theta_per_day(self) -> float:
        return self.theta / 365.0

    def scaled(self, factor: float) -> Greeks:
        """Every Greek times ``factor`` (quantity x multiplier for a position)."""
        return Greeks(
            delta=self.delta * factor,
            gamma=self.gamma * factor,
            vega=self.vega * factor,
            theta=self.theta * factor,
            rho=self.rho * factor,
        )

    def __add__(self, other: Greeks) -> Greeks:
        return Greeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            vega=self.vega + other.vega,
            theta=self.theta + other.theta,
            rho=self.rho + other.rho,
        )


ZERO_GREEKS = Greeks(0.0, 0.0, 0.0, 0.0, 0.0)


def inputs_for(
    contract: OptionContract,
    as_of: date,
    *,
    spot: float,
    vol: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> PricingInputs:
    """The pricing inputs of ``contract`` on ``as_of``."""
    return PricingInputs(
        right=contract.right,
        spot=spot,
        strike=contract.strike,
        time=contract.year_fraction(as_of),
        rate=rate,
        dividend_yield=dividend_yield,
        vol=vol,
    )


class PricingModel(ABC):
    """Prices, Greeks and implied volatility of vanilla options."""

    name: ClassVar[str]
    style: ClassVar[ModelStyle]

    @abstractmethod
    def price(self, inputs: PricingInputs) -> float:
        """The option value per share."""

    def greeks(self, inputs: PricingInputs) -> Greeks:
        """Finite-difference Greeks (central in spot, vol and rate, one
        calendar day forward in time). Models with analytic Greeks
        override this."""
        return finite_difference_greeks(self.price, inputs)

    def implied_vol(self, inputs: PricingInputs, price: float) -> float | None:
        """The vol at which :meth:`price` equals ``price``, or ``None`` when
        no vol in ``[MIN_VOL, MAX_VOL]`` does."""
        return solve_implied_vol(self.price, inputs, price)


def finite_difference_greeks(
    price_fn: Callable[[PricingInputs], float], inputs: PricingInputs
) -> Greeks:
    ds = inputs.spot * _SPOT_BUMP
    up = price_fn(replace(inputs, spot=inputs.spot + ds))
    mid = price_fn(inputs)
    down = price_fn(replace(inputs, spot=inputs.spot - ds))
    delta = (up - down) / (2 * ds)
    gamma = (up - 2 * mid + down) / (ds * ds)
    dv = min(_VOL_BUMP, inputs.vol) if inputs.vol > 0 else _VOL_BUMP
    vega = (
        price_fn(replace(inputs, vol=inputs.vol + dv))
        - price_fn(replace(inputs, vol=max(inputs.vol - dv, 0.0)))
    ) / (inputs.vol + dv - max(inputs.vol - dv, 0.0))
    rho = (
        price_fn(replace(inputs, rate=inputs.rate + _RATE_BUMP))
        - price_fn(replace(inputs, rate=inputs.rate - _RATE_BUMP))
    ) / (2 * _RATE_BUMP)
    if inputs.time > _TIME_BUMP:
        theta = (price_fn(replace(inputs, time=inputs.time - _TIME_BUMP)) - mid) / _TIME_BUMP
    else:
        theta = (inputs.intrinsic - mid) / max(inputs.time, _TIME_BUMP)
    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


def solve_implied_vol(
    price_fn: Callable[[PricingInputs], float], inputs: PricingInputs, price: float
) -> float | None:
    """Bracketed root solve (Brent) of ``price_fn(vol) = price``."""
    if not math.isfinite(price) or price <= 0 or inputs.time <= 0:
        return None
    lo, hi = inputs.with_vol(MIN_VOL), inputs.with_vol(MAX_VOL)
    f_lo = price_fn(lo) - price
    f_hi = price_fn(hi) - price
    if abs(f_lo) <= IV_TOLERANCE:
        return MIN_VOL
    if f_lo > 0 or f_hi < 0:
        return None
    root: Any = brentq(lambda v: price_fn(inputs.with_vol(v)) - price, MIN_VOL, MAX_VOL, xtol=1e-10)
    return float(root)


# ---- registry -----------------------------------------------------------------

_MODELS: dict[str, type[PricingModel]] = {}


def register_pricing_model(cls: type[PricingModel]) -> type[PricingModel]:
    """Class decorator adding a model to the registry. Names are unique."""
    existing = _MODELS.get(cls.name)
    if existing is not None and existing.__qualname__ != cls.__qualname__:
        raise ValueError(f"pricing model {cls.name!r} is already registered")
    _MODELS[cls.name] = cls
    return cls


def _discover() -> None:
    import stonks.options.pricing as package

    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("_") and info.name != "base":
            importlib.import_module(f"{package.__name__}.{info.name}")


def pricing_models() -> dict[str, type[PricingModel]]:
    """Every registered model by name, sorted."""
    _discover()
    return dict(sorted(_MODELS.items()))


def pricing_model(name: str) -> PricingModel:
    """A fresh instance of the model called ``name``."""
    models = pricing_models()
    if name not in models:
        raise ValueError(f"unknown pricing model {name!r}; choose one of {sorted(models)}")
    return models[name]()


def default_model_for(contract: OptionContract) -> PricingModel:
    """Black-Scholes for European contracts, the Barone-Adesi-Whaley
    approximation for American ones."""
    return pricing_model("black_scholes" if contract.style == "european" else "american_baw")
