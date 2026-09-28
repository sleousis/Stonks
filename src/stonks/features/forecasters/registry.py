"""Forecaster registry (roadmap 23.11).

Every public :class:`~stonks.features.forecasters.base.Forecaster` subclass
with a ``name`` in a public module of ``stonks.features.forecasters`` is
registered under that name. A new model is one new module; no list is
edited. Discovery refuses two rules breakers:

- a pretrained model with no cutoff (neither ``pretrain_cutoff`` nor
  ``release_date``), because the lab could not tell which windows it saw;
- weights under a non-commercial licence (TimesFM 3.0, Moirai), because
  Stonks trades.

Importing this module never imports a model package: adapters import them
inside their methods.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import date
from functools import cache
from types import ModuleType
from typing import Any

from stonks.features.forecasters.base import Forecaster

__all__ = [
    "build_forecaster",
    "discover_forecasters",
    "forecaster_classes",
    "forecaster_names",
    "get_forecaster_class",
    "model_cutoff",
]

_SKIPPED_MODULES = frozenset({"base", "registry", "cutoff"})
#: Licence ids (lower case, fragments) that forbid commercial use.
_NON_COMMERCIAL = ("-nc", "noncommercial", "non-commercial")


def _check(cls: type[Forecaster]) -> None:
    if cls.pretrained and cls.cutoff() is None:
        raise ValueError(
            f"forecaster {cls.name!r} is pretrained but declares no cutoff: set "
            "pretrain_cutoff (published) or release_date (the weights' release)"
        )
    licence = cls.licence.lower()
    if any(frag in licence for frag in _NON_COMMERCIAL):
        raise ValueError(
            f"forecaster {cls.name!r} has weights under the non-commercial licence "
            f"{cls.licence!r}: Stonks trades, so it cannot use them"
        )


def discover_forecasters(package: ModuleType) -> dict[str, type[Forecaster]]:
    """``name -> class`` for every forecaster in ``package``'s public
    modules, sorted by name. Raises on a duplicate name or a rule breaker."""
    found: dict[str, type[Forecaster]] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name in _SKIPPED_MODULES:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or cls.__name__.startswith("_"):
                continue
            if not issubclass(cls, Forecaster) or inspect.isabstract(cls):
                continue
            name = cls.__dict__.get("name")
            if not isinstance(name, str) or not name:
                continue
            _check(cls)
            if name in found and found[name] is not cls:
                raise ValueError(
                    f"forecaster name {name!r} is used by both "
                    f"{found[name].__qualname__} and {cls.__qualname__}"
                )
            found[name] = cls
    return dict(sorted(found.items()))


@cache
def _default() -> dict[str, type[Forecaster]]:
    import stonks.features.forecasters as package

    return discover_forecasters(package)


def forecaster_classes() -> dict[str, type[Forecaster]]:
    return dict(_default())


def forecaster_names() -> list[str]:
    return list(_default())


def get_forecaster_class(name: str) -> type[Forecaster]:
    classes = _default()
    if name not in classes:
        raise ValueError(f"unknown forecaster {name!r}; choose from {sorted(classes)}")
    return classes[name]


def build_forecaster(name: str, **options: Any) -> Forecaster:
    """An instance of the named forecaster. Pretrained models load their
    weights on first use, not here."""
    return get_forecaster_class(name)(**options)


def model_cutoff(name: str) -> date | None:
    """The named model's cutoff (see :meth:`Forecaster.cutoff`)."""
    return get_forecaster_class(name).cutoff()
