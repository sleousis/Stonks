"""Registries of universe providers and index sources.

Every concrete :class:`~stonks.universes.base.UniverseProvider` subclass in
a :mod:`stonks.universes.providers` module is a provider, keyed by its
``kind``. Every concrete
:class:`~stonks.universes.index_sources.base.IndexSource` subclass in a
:mod:`stonks.universes.index_sources` module is an index source, keyed by
its ``source_id``. Adding one is one new module; no list is edited.
Modules starting with ``_`` and ``base`` are skipped.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from functools import cache
from types import ModuleType
from typing import Any

from stonks.universes.base import UniverseProvider


def _discover(package: ModuleType, base: type, key: str) -> dict[str, type]:
    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__ or not issubclass(obj, base):
                continue
            if inspect.isabstract(obj):
                continue
            ident = getattr(obj, key)
            if ident in found:
                raise RuntimeError(f"duplicate {key} {ident!r}: {found[ident]} and {obj}")
            found[ident] = obj
    return found


@cache
def _providers() -> dict[str, UniverseProvider]:
    from stonks.universes import providers

    return {k: cls() for k, cls in _discover(providers, UniverseProvider, "kind").items()}


def provider_kinds() -> list[str]:
    return sorted(_providers())


def provider_for(kind: str) -> UniverseProvider:
    try:
        return _providers()[kind]
    except KeyError:
        raise KeyError(
            f"unknown universe kind {kind!r}; choose one of {provider_kinds()}"
        ) from None


@cache
def _index_source_classes() -> dict[str, type]:
    from stonks.universes import index_sources
    from stonks.universes.index_sources.base import IndexSource

    return _discover(index_sources, IndexSource, "source_id")


def index_source_ids() -> list[str]:
    return sorted(_index_source_classes())


def build_index_source(source_id: str, **kwargs: Any) -> Any:
    """The index source ``source_id`` (``KeyError`` when unknown)."""
    try:
        cls = _index_source_classes()[source_id]
    except KeyError:
        raise KeyError(
            f"unknown index source {source_id!r}; choose one of {index_source_ids()}"
        ) from None
    return cls(**kwargs)
