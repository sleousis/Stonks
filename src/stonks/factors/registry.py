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
from collections.abc import Sequence
from functools import cache
from typing import Any, cast

import stonks.factors.library as _library
from stonks.factors.base import ExpressionFactor, Factor

__all__ = ["factor_catalog", "factor_sets", "get_factor", "resolve_factor", "resolve_factors"]


@cache
def _library_modules() -> tuple[tuple[str, tuple[Factor, ...]], ...]:
    """``(module name, factors)`` for every library module, in module order."""
    out: list[tuple[str, tuple[Factor, ...]]] = []
    seen: set[str] = set()
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
            if factor.id in seen:
                raise ValueError(f"factor id {factor.id!r} is defined twice")
            seen.add(factor.id)
        out.append((info.name, tuple(made)))
    return tuple(out)


@cache
def _discover() -> dict[str, Factor]:
    found = {f.id: f for _, made in _library_modules() for f in made}
    return dict(sorted(found.items(), key=lambda kv: (kv[1].family, kv[0])))


def factor_sets() -> dict[str, list[str]]:
    """``set name -> factor ids``: one set per library module (``alpha158``,
    ``classic``, ``fundamentals``), ids in the module's order."""
    return {name: [f.id for f in made] for name, made in _library_modules()}


def split_top_level(text: str) -> list[str]:
    """``text`` split on commas outside parentheses, so a formula such as
    ``Ref($close, 5)`` stays whole."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def resolve_factors(spec: str | Sequence[str]) -> list[Factor]:
    """Factors named by ``spec``: comma-separated (or a list of) set names,
    library ids and formulas, in order, without repeats."""
    parts = split_top_level(spec) if isinstance(spec, str) else list(spec)
    sets = factor_sets()
    out: dict[str, Factor] = {}
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if part in sets:
            for fid in sets[part]:
                out.setdefault(fid, get_factor(fid))
            continue
        factor = resolve_factor(part)
        out.setdefault(factor.id, factor)
    if not out:
        raise ValueError("name at least one factor, factor set or formula")
    return list(out.values())


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
