"""The portfolio-construction seam (BL-08, principle P30).

Strategies choose names and convictions; a :class:`PortfolioConstructor`
chooses sizes. It turns a :class:`ConstructionInput` (per-strategy signals
already normalised by :mod:`stonks.portfolio.signals`, plus the book,
prices and volatilities) into a :class:`TargetBook` of target weights, which
:func:`stonks.portfolio.orders.orders_from_targets` diffs into orders. The
risk layer only ever cuts what comes out.

Constructors are found by name through a registry: decorate a subclass with
``@register_constructor("name")`` in any public module of this package and
:func:`get_constructor` finds it. Adding a constructor is one new file.

Each constructor declares:

- ``Settings``: a pydantic model of its knobs (validated, unknown keys
  rejected); ``get_constructor(name, **settings)`` builds it.
- ``signal_method``: the :func:`~stonks.portfolio.signals.normalize` method
  its signals must be normalised with (``"raw"`` for none), and optionally
  ``long_short_signal_method`` for its long/short mode.

Long/short books (roadmap 16.3): with ``long_only=False`` weights may be
negative and ``max_gross`` may exceed 1.0 (a margin account; a cash
account stays at 1.0, principle P26). :meth:`PortfolioConstructor.finalize`
then makes the book neutral when asked (``neutral="dollar"``: the larger
leg shrinks to the smaller; ``"beta"``: the leg with more beta exposure
shrinks, on constructors that read betas), caps gross at ``max_gross``,
and keeps net inside ``[min_net, max_net]`` by shrinking one leg. Every
step only shrinks weights. A long-only book is finalised exactly as before.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.core.types import AssetClass, Portfolio

StrategyId = str
Ticker = str
Neutrality = Literal["none", "dollar", "beta"]

#: Highest gross a long/short book may target (e.g. 130/30 is 1.6, a
#: market-neutral 100/100 book 2.0).
MAX_LONG_SHORT_GROSS = 4.0
#: Rows of overlapping returns needed before a beta is estimated.
MIN_BETA_OBSERVATIONS = 20


def estimate_betas(
    returns_history: pd.DataFrame | None, *, min_observations: int = MIN_BETA_OBSERVATIONS
) -> dict[Ticker, float]:
    """Beta of each column of ``returns_history`` against the equal-weight
    mean of all columns (a market proxy built from the book's own
    universe): ``cov(r_i, m) / var(m)`` over the rows where both are
    present. Columns with fewer than ``min_observations`` such rows, or a
    flat market, get no beta."""
    if returns_history is None or returns_history.empty:
        return {}
    market = returns_history.mean(axis=1, skipna=True)
    out: dict[Ticker, float] = {}
    for column in returns_history.columns:
        pair = pd.concat([returns_history[column], market], axis=1).dropna()
        if len(pair) < min_observations:
            continue
        x = pair.iloc[:, 0].to_numpy(dtype=float)
        m = pair.iloc[:, 1].to_numpy(dtype=float)
        var = float(np.var(m, ddof=1))
        if not var > 0:
            continue
        beta = float(np.cov(x, m, ddof=1)[0, 1] / var)
        if math.isfinite(beta):
            out[str(column)] = beta
    return out


@dataclass(frozen=True, kw_only=True)
class ConstructionInput:
    """Everything a constructor may look at, as of one decision date.

    - ``signals``: ``strategy_id -> ticker -> score``, normalised with the
      constructor's ``signal_method``. A missing ticker means "no opinion".
    - ``strategy_weights``: relative capital per strategy; ``None`` means
      equal. Normalised to sum to 1 by :meth:`normalized_strategy_weights`.
    - ``vols_annual``: annualised sigma per ticker (see
      :mod:`stonks.features.volatility`).
    - ``forecast_history``: optional past forecasts, one column per strategy
      (one row per observation), for the forecast diversification multiplier.
    - ``returns_history``: optional past per-bar returns, one column per
      ticker, for the instrument diversification multiplier.
    - ``betas``: optional beta per ticker for beta-neutral books; missing
      ones are estimated from ``returns_history`` (:func:`estimate_betas`),
      else taken as 1.0.
    """

    signals: Mapping[StrategyId, Mapping[Ticker, float]]
    portfolio: Portfolio
    prices: Mapping[Ticker, float]
    as_of: date
    strategy_weights: Mapping[StrategyId, float] | None = None
    vols_annual: Mapping[Ticker, float] = field(default_factory=dict)
    asset_classes: Mapping[Ticker, AssetClass] = field(default_factory=dict)
    forecast_history: pd.DataFrame | None = None
    returns_history: pd.DataFrame | None = None
    betas: Mapping[Ticker, float] = field(default_factory=dict)

    def betas_for(self, tickers: Iterable[Ticker]) -> dict[Ticker, float]:
        """Beta per ticker: given, else estimated, else 1.0."""
        wanted = list(tickers)
        estimated: dict[Ticker, float] = {}
        if any(t not in self.betas for t in wanted):
            estimated = estimate_betas(self.returns_history)
        out: dict[Ticker, float] = {}
        for t in wanted:
            beta = self.betas.get(t, estimated.get(t, 1.0))
            out[t] = float(beta) if beta is not None and math.isfinite(beta) else 1.0
        return out

    def normalized_strategy_weights(self) -> dict[StrategyId, float]:
        """Weight per strategy in ``signals``, summing to 1 (equal by default;
        strategies absent from ``strategy_weights`` get 0)."""
        ids = list(self.signals)
        if not ids:
            return {}
        if self.strategy_weights is None:
            return {sid: 1.0 / len(ids) for sid in ids}
        raw = {sid: float(self.strategy_weights.get(sid, 0.0)) for sid in ids}
        if any(w < 0 or not math.isfinite(w) for w in raw.values()):
            raise ValueError(f"strategy weights must be finite and >= 0, got {raw}")
        total = sum(raw.values())
        if total <= 0:
            raise ValueError("strategy weights sum to 0")
        return {sid: w / total for sid, w in raw.items()}

    def tradable(self, ticker: Ticker) -> bool:
        price = self.prices.get(ticker)
        return price is not None and math.isfinite(price) and price > 0

    def vol(self, ticker: Ticker) -> float | None:
        """Positive finite annual sigma, or ``None`` when unusable."""
        sigma = self.vols_annual.get(ticker)
        if sigma is None or not math.isfinite(sigma) or sigma <= 0:
            return None
        return float(sigma)


@dataclass(frozen=True)
class TargetBook:
    """Target weights (fractions of total equity, positive = long) plus, per
    ticker, each strategy's share of that weight (shares sum to 1).
    Tickers held but absent from ``weights`` have a target of 0."""

    weights: dict[Ticker, float] = field(default_factory=dict)
    attribution: dict[Ticker, dict[StrategyId, float]] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        bad = {t: w for t, w in self.weights.items() if not math.isfinite(w)}
        if bad:
            raise ValueError(f"non-finite target weights: {bad}")

    @property
    def gross(self) -> float:
        return sum(abs(w) for w in self.weights.values())

    @property
    def net(self) -> float:
        return sum(self.weights.values())

    @property
    def cash_weight(self) -> float:
        return 1.0 - self.net


class ConstructorSettings(BaseModel):
    """Knobs every constructor shares. A cash account is long-only and never
    above 1.0 gross (principle P26). ``min_net``, ``max_net`` and
    ``neutral`` only apply in long/short mode (``long_only=False``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    long_only: bool = True
    max_gross: float = Field(default=1.0, gt=0.0, le=MAX_LONG_SHORT_GROSS)
    min_net: float = Field(default=-1.0, ge=-MAX_LONG_SHORT_GROSS, le=MAX_LONG_SHORT_GROSS)
    max_net: float = Field(default=1.0, ge=-MAX_LONG_SHORT_GROSS, le=MAX_LONG_SHORT_GROSS)
    neutral: Neutrality = "none"

    @model_validator(mode="after")
    def _limits(self) -> ConstructorSettings:
        if self.long_only and self.max_gross > 1.0:
            raise ValueError(
                f"a long-only book never runs above 1.0 gross (P26), got max_gross={self.max_gross}"
            )
        if self.min_net > self.max_net:
            raise ValueError(f"min_net {self.min_net} must be <= max_net {self.max_net}")
        return self


class PortfolioConstructor(ABC):
    """Turns normalised signals into target weights. Subclasses set
    ``Settings`` and ``signal_method`` and implement :meth:`target_weights`;
    :meth:`finalize` applies the shared long-only and gross caps."""

    name: ClassVar[str] = ""
    signal_method: ClassVar[str] = "zscore"
    #: Normalisation in long/short mode; ``None`` keeps ``signal_method``.
    long_short_signal_method: ClassVar[str | None] = None
    #: Passes betas to :meth:`finalize`, so ``neutral="beta"`` works.
    beta_aware: ClassVar[bool] = False
    Settings: ClassVar[type[ConstructorSettings]] = ConstructorSettings

    def __init__(self, settings: ConstructorSettings | None = None) -> None:
        self.settings = settings if settings is not None else self.Settings()
        if self.settings.neutral == "beta" and not self.beta_aware:
            raise ValueError(f"constructor {self.name!r} cannot make a book beta neutral")

    @abstractmethod
    def target_weights(self, inp: ConstructionInput) -> TargetBook: ...

    def normalization(self) -> str:
        """The signal normalisation this constructor's mode needs."""
        if not self.settings.long_only and self.long_short_signal_method is not None:
            return self.long_short_signal_method
        return self.signal_method

    def finalize(
        self,
        weights: Mapping[Ticker, float],
        attribution: Mapping[Ticker, Mapping[StrategyId, float]] | None = None,
        meta: Mapping[str, Any] | None = None,
        *,
        betas: Mapping[Ticker, float] | None = None,
    ) -> TargetBook:
        """Drop non-finite and zero weights, clip shorts when long-only, then
        scale the whole book down pro rata if gross exceeds ``max_gross``.
        In long/short mode it first applies ``neutral`` (``betas`` for
        ``"beta"``) and last the net limits (see the module doc)."""
        s = self.settings
        clean: dict[Ticker, float] = {}
        for ticker, w in weights.items():
            w = float(w)
            if not math.isfinite(w):
                continue
            if s.long_only:
                w = max(w, 0.0)
            if w != 0.0:
                clean[ticker] = w
        if not s.long_only and s.neutral != "none":
            clean = _neutralise(clean, s.neutral, betas or {})
        gross = sum(abs(w) for w in clean.values())
        if gross > s.max_gross:
            scale = s.max_gross / gross
            clean = {t: w * scale for t, w in clean.items()}
        if not s.long_only:
            clean = _net_limits(clean, s.min_net, s.max_net)
        attr = {t: dict(attribution[t]) for t in clean if attribution and t in attribution}
        return TargetBook(weights=clean, attribution=attr, meta=dict(meta or {}))


def _scale_leg(weights: dict[Ticker, float], long_leg: bool, k: float) -> dict[Ticker, float]:
    """``weights`` with one leg multiplied by ``k`` (0 drops it)."""
    out: dict[Ticker, float] = {}
    for t, w in weights.items():
        if (w > 0) == long_leg:
            w *= k
        if w != 0.0:
            out[t] = w
    return out


def _neutralise(
    weights: dict[Ticker, float], neutral: Neutrality, betas: Mapping[Ticker, float]
) -> dict[Ticker, float]:
    """Shrink the leg with more exposure (dollars, or beta-weighted dollars
    with betas floored at 0) until both legs match."""

    def exposure(t: Ticker, w: float) -> float:
        if neutral == "dollar":
            return abs(w)
        return abs(w) * max(float(betas.get(t, 1.0)), 0.0)

    longs = sum(exposure(t, w) for t, w in weights.items() if w > 0)
    shorts = sum(exposure(t, w) for t, w in weights.items() if w < 0)
    if longs > shorts:
        return _scale_leg(weights, True, shorts / longs)
    if shorts > longs:
        return _scale_leg(weights, False, longs / shorts)
    return weights


def _net_limits(
    weights: dict[Ticker, float], min_net: float, max_net: float
) -> dict[Ticker, float]:
    """Keep ``sum(w)`` in ``[min_net, max_net]`` by shrinking the long leg
    (net too high) or the short leg (net too low)."""
    longs = sum(w for w in weights.values() if w > 0)
    shorts = -sum(w for w in weights.values() if w < 0)
    net = longs - shorts
    if net > max_net and longs > 0:
        return _scale_leg(weights, True, max(max_net + shorts, 0.0) / longs)
    if net < min_net and shorts > 0:
        return _scale_leg(weights, False, max(longs - min_net, 0.0) / shorts)
    return weights


def combine_signals(
    inp: ConstructionInput,
) -> tuple[dict[Ticker, float], dict[Ticker, dict[StrategyId, float]]]:
    """``sum_j s_j * signal_ij`` per ticker, and each strategy's share of that
    sum (signed; shares sum to 1 wherever the sum is non-zero)."""
    weights = inp.normalized_strategy_weights()
    contributions: dict[Ticker, dict[StrategyId, float]] = {}
    for sid, scores in inp.signals.items():
        s = weights.get(sid, 0.0)
        if s == 0.0:
            continue
        for ticker, score in scores.items():
            if score is None or not math.isfinite(score):
                continue
            contributions.setdefault(ticker, {})[sid] = s * float(score)
    combined = {t: sum(c.values()) for t, c in contributions.items()}
    attribution = {
        t: {sid: c / combined[t] for sid, c in contribs.items()}
        for t, contribs in contributions.items()
        if combined[t] != 0.0
    }
    return combined, attribution


# --- registry -----------------------------------------------------------------

_REGISTRY: dict[str, type[PortfolioConstructor]] = {}
_DISCOVERED: set[str] = set()


def register_constructor(
    name: str,
) -> Callable[[type[PortfolioConstructor]], type[PortfolioConstructor]]:
    """Class decorator: register a :class:`PortfolioConstructor` under ``name``."""

    def decorate(cls: type[PortfolioConstructor]) -> type[PortfolioConstructor]:
        existing = _REGISTRY.get(name)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"constructor {name!r} is already registered by {existing!r}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorate


def discover_constructors(package: str = __package__ or "stonks.portfolio") -> None:
    """Import every public module of ``package`` so its
    ``@register_constructor`` decorators run. Idempotent."""
    if package in _DISCOVERED:
        return
    pkg = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{package}.{info.name}")
    _DISCOVERED.add(package)


def constructor_names() -> list[str]:
    """Every registered constructor name, sorted."""
    discover_constructors()
    return sorted(_REGISTRY)


def get_constructor(name: str, **settings: Any) -> PortfolioConstructor:
    """Build the constructor registered as ``name`` with validated
    ``settings``. An unknown name raises ``ValueError`` listing valid ones."""
    discover_constructors()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"unknown portfolio constructor {name!r}; choose one of {sorted(_REGISTRY)}"
        )
    return cls(cls.Settings(**settings))
