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
  its signals must be normalised with (``"raw"`` for none).
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import AssetClass, Portfolio

StrategyId = str
Ticker = str


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
    above 1.0 gross (principle P26)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    long_only: bool = True
    max_gross: float = Field(default=1.0, gt=0.0, le=1.0)


class PortfolioConstructor(ABC):
    """Turns normalised signals into target weights. Subclasses set
    ``Settings`` and ``signal_method`` and implement :meth:`target_weights`;
    :meth:`finalize` applies the shared long-only and gross caps."""

    name: ClassVar[str] = ""
    signal_method: ClassVar[str] = "zscore"
    Settings: ClassVar[type[ConstructorSettings]] = ConstructorSettings

    def __init__(self, settings: ConstructorSettings | None = None) -> None:
        self.settings = settings if settings is not None else self.Settings()

    @abstractmethod
    def target_weights(self, inp: ConstructionInput) -> TargetBook: ...

    def finalize(
        self,
        weights: Mapping[Ticker, float],
        attribution: Mapping[Ticker, Mapping[StrategyId, float]] | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> TargetBook:
        """Drop non-finite and zero weights, clip shorts when long-only, then
        scale the whole book down pro rata if gross exceeds ``max_gross``."""
        clean: dict[Ticker, float] = {}
        for ticker, w in weights.items():
            w = float(w)
            if not math.isfinite(w):
                continue
            if self.settings.long_only:
                w = max(w, 0.0)
            if w != 0.0:
                clean[ticker] = w
        gross = sum(abs(w) for w in clean.values())
        if gross > self.settings.max_gross:
            scale = self.settings.max_gross / gross
            clean = {t: w * scale for t, w in clean.items()}
        attr = {t: dict(attribution[t]) for t in clean if attribution and t in attribution}
        return TargetBook(weights=clean, attribution=attr, meta=dict(meta or {}))


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
