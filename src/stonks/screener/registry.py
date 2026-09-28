"""The screen metric registry: every concrete
:class:`~stonks.screener.metrics.base.ScreenMetric` subclass with an ``id``
in a :mod:`stonks.screener.metrics` module. Adding one is one new class;
no list is edited. Modules starting with ``_`` and ``base`` are skipped."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from functools import cache

from stonks.screener.metrics.base import ScreenMetric


@cache
def _metrics() -> dict[str, ScreenMetric]:
    from stonks.screener import metrics

    found: dict[str, ScreenMetric] = {}
    for info in pkgutil.iter_modules(metrics.__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        module = importlib.import_module(f"{metrics.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not issubclass(cls, ScreenMetric):
                continue
            ident = getattr(cls, "id", None)
            if ident is None or inspect.isabstract(cls):
                continue
            if ident in found:
                raise RuntimeError(f"duplicate screen metric {ident!r}")
            found[ident] = cls()
    return dict(sorted(found.items()))


def metric_ids() -> list[str]:
    return list(_metrics())


def all_metrics() -> list[ScreenMetric]:
    return list(_metrics().values())


def metric_for(metric_id: str) -> ScreenMetric:
    try:
        return _metrics()[metric_id]
    except KeyError:
        raise KeyError(f"unknown metric {metric_id!r}; choose one of {metric_ids()}") from None
