"""Strategy catalog: the strategy classes ``stonks lab run`` can name.

Every concrete strategy defined in a public ``stonks.strategies.examples``
module (modules starting with ``_`` hold shared helpers and are skipped),
plus the wrapper strategies (:class:`MacroRegimeFilter`,
:class:`FeatureRegimeFilter`, :class:`LastTradeFilter`,
:class:`RegimeFilter`, :class:`TrailingStopWrapper`,
:class:`LatentRegimeFilter`), keyed by ``id``. ``resolve_strategy``
also accepts a class name or a ``module:Class`` path, and
``load_strategy_class`` returns the same class typed as a tradable
:class:`~stonks.core.protocols.Strategy` for the lab.

Outside packages add strategies through the ``stonks.strategies`` entry
point group (``lab.plugins``). Only classes named by an installed entry
point are imported, never an arbitrary ``module:Class`` from a request, and
a plugin never replaces a built-in id.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TYPE_CHECKING, cast

import stonks.strategies.examples as _examples
from stonks.lab import plugins
from stonks.logging import get_logger
from stonks.strategies.base import BaseStrategy
from stonks.strategies.feature_regime import FeatureRegimeFilter
from stonks.strategies.last_trade_filter import LastTradeFilter
from stonks.strategies.latent_regime import LatentRegimeFilter
from stonks.strategies.macro_regime import MacroRegimeFilter
from stonks.strategies.regime import RegimeFilter
from stonks.strategies.trailing_stop import TrailingStopWrapper

if TYPE_CHECKING:  # pragma: no cover
    from stonks.core.protocols import Strategy

_log = get_logger("stonks.lab.catalog")

_WRAPPERS: tuple[type[BaseStrategy], ...] = (
    MacroRegimeFilter,
    FeatureRegimeFilter,
    LastTradeFilter,
    RegimeFilter,
    TrailingStopWrapper,
    LatentRegimeFilter,
)


def strategy_catalog() -> dict[str, type[BaseStrategy]]:
    """``id -> class`` for every catalogued strategy, sorted by id."""
    found: dict[str, type[BaseStrategy]] = {cls.id: cls for cls in _WRAPPERS}
    for info in pkgutil.iter_modules(_examples.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_examples.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, BaseStrategy)
                and cls.__module__ == module.__name__
                and not cls.__name__.startswith("_")
            ):
                found[cls.id] = cls
    for cls in _plugin_strategies():
        if cls.id in found and found[cls.id] is not cls:
            _log.warning("lab.plugin.name_taken", group=plugins.STRATEGIES, name=cls.id)
            continue
        found[cls.id] = cls
    return dict(sorted(found.items()))


def _plugin_strategies() -> list[type[BaseStrategy]]:
    """The strategy classes of the ``stonks.strategies`` entry points. An
    entry point naming anything else is logged and skipped."""
    out: list[type[BaseStrategy]] = []
    for plugin in plugins.plugin_objects(plugins.STRATEGIES):
        cls = plugin.obj
        if (
            isinstance(cls, type)
            and issubclass(cls, BaseStrategy)
            and isinstance(cls.__dict__.get("id"), str)
        ):
            out.append(cls)
        else:
            _log.warning("lab.plugin.not_a_strategy", name=plugin.name, value=repr(cls))
    return out


def is_wrapper(cls: type) -> bool:
    """True for a wrapper strategy (it needs an inner strategy to run)."""
    return cls in _WRAPPERS


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


def load_strategy_class(name: str) -> type[Strategy]:
    """``resolve_strategy`` for code that runs the strategy (the lab, a
    sweep). Checks the class can rank and decide, so it satisfies the
    ``Strategy`` protocol. Raises ``ValueError`` when it cannot."""
    cls = resolve_strategy(name)
    missing = [m for m in ("estimate_return", "decide") if not callable(getattr(cls, m, None))]
    if missing:
        raise ValueError(f"strategy {cls.id!r} cannot trade: it has no {', '.join(missing)}")
    return cast("type[Strategy]", cls)
