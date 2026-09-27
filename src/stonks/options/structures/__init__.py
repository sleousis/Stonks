"""The structure registry (roadmap 17.5, design section 6).

A structure turns an :class:`~stonks.options.strategy.OptionIntent` into a
:class:`~stonks.options.orders.ComboOrder`, picking its contracts from the
day's chain with a :class:`~stonks.options.selector.LegSelector`. Every
module in this package is imported on the first lookup and each function
decorated with ``@register_structure(name)`` joins the registry, so a new
structure is one new module.

A builder returns ``None`` when the chain cannot supply the legs (no
expiry in the window, no liquid strike): the intent is then skipped and
logged, never filled at a guess.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stonks.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from stonks.options.orders import ComboOrder
    from stonks.options.selector import LegSelector
    from stonks.options.strategy import OptionDecisionContext, OptionIntent

_log = get_logger("stonks.options.structures")


@dataclass(frozen=True)
class BuildRequest:
    intent: OptionIntent
    ctx: OptionDecisionContext
    selector: LegSelector
    client_id: str
    strategy_id: str | None = None


Builder = Callable[[BuildRequest], "ComboOrder | None"]

_STRUCTURES: dict[str, Builder] = {}


def register_structure(name: str) -> Callable[[Builder], Builder]:
    def add(fn: Builder) -> Builder:
        existing = _STRUCTURES.get(name)
        if existing is not None and existing.__qualname__ != fn.__qualname__:
            raise ValueError(f"structure {name!r} is already registered")
        _STRUCTURES[name] = fn
        return fn

    return add


def structures() -> dict[str, Builder]:
    import stonks.options.structures as package

    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{package.__name__}.{info.name}")
    return dict(sorted(_STRUCTURES.items()))


def build(request: BuildRequest) -> ComboOrder | None:
    registry = structures()
    name = request.intent.structure
    if name not in registry:
        raise ValueError(f"unknown structure {name!r}; choose one of {sorted(registry)}")
    combo = registry[name](request)
    if combo is None:
        _log.info(
            "options.structure.skipped",
            structure=name,
            underlying=request.intent.underlying,
            as_of=str(request.ctx.as_of),
        )
    return combo
