"""The forecast-weight seam (roadmap 22.7).

A :class:`ForecastWeightEstimator` turns the past net-of-cost returns of
several trading rules on one instrument into long-only weights that sum to
1 (Carver's forecast weights). It sees only the training rows the caller
hands it (principle P12); :func:`stonks.features.forecast_weights.fit.
fit_forecast_weights` builds those rows, applies the speed limit first and
hands over the rules that survive it.

Estimators are found by name: decorate a subclass with
``@register_weight_estimator("name")`` in any public module of this
package and :func:`get_weight_estimator` finds it. A new estimator is one
new file.
"""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd


@dataclass(frozen=True)
class WeightInput:
    """What an estimator sees.

    ``net_returns``: one column per rule, the per-bar return of trading the
    rule's forecast after costs, over training rows only (every row has a
    value for every column). ``cost_sr``: each rule's yearly cost in Sharpe
    units (turnover times cost per trade over annual volatility).
    """

    net_returns: pd.DataFrame
    cost_sr: Mapping[str, float] = field(default_factory=dict)

    @property
    def rules(self) -> list[str]:
        return [str(c) for c in self.net_returns.columns]


class ForecastWeightEstimator(ABC):
    """Long-only forecast weights for one instrument's rules."""

    name: ClassVar[str] = ""

    @abstractmethod
    def estimate(self, inp: WeightInput) -> dict[str, float]:
        """``rule -> weight``, every weight ``>= 0``, summing to 1."""


def normalise(weights: Mapping[str, float]) -> dict[str, float]:
    """Clip negatives to 0 and rescale to sum 1; equal weights when nothing
    is left."""
    clipped = {k: max(0.0, float(v)) for k, v in weights.items()}
    total = sum(clipped.values())
    if not total > 0:
        return {k: 1.0 / len(clipped) for k in clipped} if clipped else {}
    return {k: v / total for k, v in clipped.items()}


_REGISTRY: dict[str, type[ForecastWeightEstimator]] = {}
_DISCOVERED: set[str] = set()


def register_weight_estimator(
    name: str,
) -> Callable[[type[ForecastWeightEstimator]], type[ForecastWeightEstimator]]:
    """Class decorator: register an estimator under ``name``."""

    def decorate(cls: type[ForecastWeightEstimator]) -> type[ForecastWeightEstimator]:
        existing = _REGISTRY.get(name)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"weight estimator {name!r} is already registered by {existing!r}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorate


def discover_weight_estimators(package: str = __package__ or "") -> None:
    """Import every public module of ``package`` so its decorators run."""
    if package in _DISCOVERED:
        return
    pkg = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{package}.{info.name}")
    _DISCOVERED.add(package)


def weight_estimator_names() -> list[str]:
    """Every registered estimator name, sorted."""
    discover_weight_estimators()
    return sorted(_REGISTRY)


def get_weight_estimator(name: str, **settings: Any) -> ForecastWeightEstimator:
    """The estimator registered as ``name``, built with ``settings``. An
    unknown name raises ``ValueError`` listing the valid ones."""
    discover_weight_estimators()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"unknown weight estimator {name!r}; choose one of {sorted(_REGISTRY)}")
    return cls(**settings)
