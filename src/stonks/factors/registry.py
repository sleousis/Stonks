"""The factor registry (roadmap 22.2): every factor the library defines,
discovered from the modules of :mod:`stonks.factors.library`.

Each public module there has a ``factors()`` function returning its
:class:`~stonks.factors.base.Factor` objects. Adding a set of factors is one
new module; no list is edited. Ids are unique across the library.

:func:`resolve_factor` also accepts a formula typed by a person, which
becomes an unnamed :class:`~stonks.factors.base.ExpressionFactor`.
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import cache
from typing import Any, cast

import stonks.factors.library as _library
from stonks.factors.base import ExpressionFactor, Factor

__all__ = ["factor_catalog", "get_factor", "resolve_factor"]


@cache
def _discover() -> dict[str, Factor]:
    found: dict[str, Factor] = {}
    for info in pkgutil.iter_modules(_library.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_library.__name__}.{info.name}")
        make: Any = getattr(module, "factors", None)
        if not callable(make):
            continue
        made: list[Any] = list(cast(Any, make)())
        for factor in made:
            if not isinstance(factor, Factor):
                raise TypeError(f"{module.__name__}.factors() returned {factor!r}")
            if factor.id in found:
                raise ValueError(f"factor id {factor.id!r} is defined twice")
            found[factor.id] = factor
    return dict(sorted(found.items(), key=lambda kv: (kv[1].family, kv[0])))


def factor_catalog() -> dict[str, Factor]:
    """``id -> factor`` for the whole library, grouped by family."""
    return dict(_discover())


def get_factor(factor_id: str) -> Factor:
    """The library factor ``factor_id``; ``ValueError`` when unknown."""
    catalog = _discover()
    if factor_id not in catalog:
        raise ValueError(f"unknown factor {factor_id!r}; see the factor catalog")
    return catalog[factor_id]


def resolve_factor(text: str) -> Factor:
    """A library factor by id, else ``text`` parsed as a formula (raising
    :class:`~stonks.factors.expression.ExpressionError` when it is neither)."""
    name = text.strip()
    catalog = _discover()
    if name in catalog:
        return catalog[name]
    return ExpressionFactor.adhoc(name)
