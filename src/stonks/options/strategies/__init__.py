"""The options strategy catalog (roadmap 17.5).

Every concrete :class:`~stonks.options.strategy.OptionStrategy` defined in
a public module of this package, keyed by ``id``. A new strategy is one
new module; nothing central is edited. None of them runs in the tick:
they are research strategies for the options backtest and its validation.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from stonks.options.strategy import OptionStrategy


def option_strategy_catalog() -> dict[str, type[OptionStrategy]]:
    import stonks.options.strategies as package

    found: dict[str, type[OptionStrategy]] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, OptionStrategy)
                and cls.__module__ == module.__name__
                and not inspect.isabstract(cls)
            ):
                found[cls.id] = cls
    return dict(sorted(found.items()))


def resolve_option_strategy(name: str) -> type[OptionStrategy]:
    catalog = option_strategy_catalog()
    if name in catalog:
        return catalog[name]
    raise ValueError(f"unknown options strategy {name!r}; choose one of {sorted(catalog)}")
