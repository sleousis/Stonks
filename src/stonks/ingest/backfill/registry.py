"""Backfill phases, discovered from :mod:`stonks.ingest.backfill.phases`.
A new phase is one new module there; no list is edited."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Collection
from functools import cache

from stonks.ingest.backfill import phases as _package
from stonks.ingest.backfill.base import BackfillPhase


@cache
def _phases() -> dict[str, BackfillPhase]:
    found: dict[str, BackfillPhase] = {}
    for info in pkgutil.iter_modules(_package.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_package.__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj.__module__ == module.__name__
                and issubclass(obj, BackfillPhase)
                and not inspect.isabstract(obj)
            ):
                if obj.phase_id in found:
                    raise RuntimeError(f"duplicate backfill phase {obj.phase_id!r}")
                found[obj.phase_id] = obj()
    return found


def all_phases() -> list[BackfillPhase]:
    """Every phase, in the order they run."""
    return sorted(_phases().values(), key=lambda p: (p.order, p.phase_id))


def select_phases(ids: Collection[str] | None) -> list[BackfillPhase]:
    """The phases named in ``ids`` (``None`` or empty: all), in run order."""
    phases = all_phases()
    if not ids:
        return phases
    known = {p.phase_id for p in phases}
    unknown = sorted(set(ids) - known)
    if unknown:
        raise ValueError(f"unknown backfill phases {unknown}; known: {sorted(known)}")
    return [p for p in phases if p.phase_id in set(ids)]
