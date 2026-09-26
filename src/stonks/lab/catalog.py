"""Strategy catalog: the strategy classes ``stonks lab run`` can name.

Every concrete strategy defined in a ``stonks.strategies.examples`` module,
plus :class:`MacroRegimeFilter`, keyed by its ``id``. ``resolve_strategy``
also accepts a class name or a ``module:Class`` path.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import stonks.strategies.examples as _examples
from stonks.strategies.base import BaseStrategy
from stonks.strategies.macro_regime import MacroRegimeFilter


def strategy_catalog() -> dict[str, type[BaseStrategy]]:
    """``id -> class`` for every catalogued strategy, sorted by id."""
    found: dict[str, type[BaseStrategy]] = {MacroRegimeFilter.id: MacroRegimeFilter}
    for info in pkgutil.iter_modules(_examples.__path__):
        module = importlib.import_module(f"{_examples.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, BaseStrategy)
                and cls.__module__ == module.__name__
                and not cls.__name__.startswith("_")
            ):
                found[cls.id] = cls
    return dict(sorted(found.items()))


def resolve_strategy(name: str) -> type[BaseStrategy]:
    """The strategy class for an id, a class name, or a ``module:Class``
    path. Raises ``ValueError`` listing the catalog on no match."""
    catalog = strategy_catalog()
    if name in catalog:
        return catalog[name]
    for cls in catalog.values():
        if name in (cls.__name__, f"{cls.__module__}:{cls.__name__}"):
            return cls
    raise ValueError(f"unknown strategy {name!r}; choose one of {sorted(catalog)}")
