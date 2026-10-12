"""Lab plugins from outside packages, through Python entry points.

A pip package can add to the lab without a change to Stonks. It names its
classes under one of the entry point groups in :data:`GROUPS`, for example
in its ``pyproject.toml``::

    [project.entry-points."stonks.strategies"]
    my_trend = "my_pkg.trend:MyTrend"

    [project.entry-points."stonks.objectives"]
    omega = "my_pkg.objectives:OmegaObjective"

Each group is read once per process and cached. Every plugin is logged by
name with its distribution. A plugin that fails to load is logged and
skipped, so one broken package never hides the rest.

This module only loads the objects. Each registry decides what it accepts
and under what name: the strategy catalog (``lab.catalog``), the objective
registry (``lab.objectives``), the tuner registry (``lab.tuning.registry``)
and the survival-test registry (``lab.survival.registry``). A plugin never
replaces a built-in of the same name. Only classes named by an installed
entry point are imported, so a request still cannot import an arbitrary
``module:Class`` (the catalog stays the allow-list).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from stonks.logging import get_logger

__all__ = [
    "GROUPS",
    "OBJECTIVES",
    "STRATEGIES",
    "SURVIVAL_TESTS",
    "TUNERS",
    "Plugin",
    "clear_cache",
    "plugin_objects",
]

_log = get_logger("stonks.lab.plugins")

STRATEGIES = "stonks.strategies"
OBJECTIVES = "stonks.objectives"
TUNERS = "stonks.tuners"
SURVIVAL_TESTS = "stonks.survival_tests"
#: Every entry point group the lab reads.
GROUPS: tuple[str, ...] = (STRATEGIES, OBJECTIVES, TUNERS, SURVIVAL_TESTS)


@dataclass(frozen=True)
class Plugin:
    """One loaded entry point: its name, the object it names and the
    distribution (pip package) that ships it."""

    group: str
    name: str
    obj: Any
    distribution: str | None


_lock = threading.Lock()
_loaded: dict[str, tuple[Plugin, ...]] = {}


def plugin_objects(group: str) -> tuple[Plugin, ...]:
    """The loaded plugins of ``group``, in entry point order. Read once and
    cached (see :func:`clear_cache`)."""
    if group not in GROUPS:
        raise ValueError(f"unknown plugin group {group!r}; choose from {list(GROUPS)}")
    with _lock:
        cached = _loaded.get(group)
        if cached is None:
            cached = _loaded[group] = _load(group)
        return cached


def clear_cache() -> None:
    """Forget the loaded plugins, so the next read loads them again."""
    with _lock:
        _loaded.clear()


def _load(group: str) -> tuple[Plugin, ...]:
    found: list[Plugin] = []
    try:
        points = list(entry_points(group=group))
    except Exception as exc:  # broken package metadata must not stop the lab
        _log.warning("lab.plugin.failed", group=group, name=None, error=str(exc))
        return ()
    for point in points:
        dist = getattr(getattr(point, "dist", None), "name", None)
        try:
            obj = point.load()
        except Exception as exc:
            _log.warning(
                "lab.plugin.failed",
                group=group,
                name=point.name,
                value=point.value,
                distribution=dist,
                error=str(exc),
            )
            continue
        _log.info(
            "lab.plugin.loaded", group=group, name=point.name, value=point.value, distribution=dist
        )
        found.append(Plugin(group=group, name=point.name, obj=obj, distribution=dist))
    return tuple(found)
