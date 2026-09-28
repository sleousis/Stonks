"""Where a trade's initial stop comes from, for its R multiple (roadmap 23.3).

A :class:`StopSource` looks at the opening order of a trade and the book's
orders and returns the stop the trade started with, or ``None``. Sources
are found automatically: every public module of this package is imported
and each class decorated with ``@register_stop_source`` joins the registry,
so a new source (a new kind of trade plan) is one new file. The first
source by ``priority`` that finds a stop wins.

Shipped sources:

- ``order_plan``: a stop written on the opening order itself (the manual
  ticket's trade plan, roadmap 23.4).
- ``protective_stop``: the first protective stop Stonks placed for the
  entry (roadmap 19.10).
"""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from stonks.journal.trips import Ledger, LedgerOrder

__all__ = ["FoundStop", "StopSource", "find_stop", "register_stop_source", "registered_sources"]


@dataclass(frozen=True)
class FoundStop:
    price: float
    #: The source's name, shown next to the R multiple.
    source: str
    #: The plan's profit target, when the source knows one.
    target: float | None = None


class StopSource(ABC):
    name: ClassVar[str]
    #: Lower runs first.
    priority: ClassVar[int] = 100

    @abstractmethod
    def find(self, entry: LedgerOrder, ledger: Ledger) -> FoundStop | None:
        """The initial stop of the trade ``entry`` opened, or ``None``."""


_REGISTRY: dict[str, type[StopSource]] = {}
_discovered = False


def register_stop_source[T: type[StopSource]](cls: T) -> T:
    _REGISTRY[cls.name] = cls
    return cls


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    import stonks.journal.stops as package

    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{package.__name__}.{info.name}")
    _discovered = True


def registered_sources() -> list[StopSource]:
    """One instance of every source, by ``priority`` then name."""
    _discover()
    return [cls() for cls in sorted(_REGISTRY.values(), key=lambda c: (c.priority, c.name))]


def find_stop(
    entry: LedgerOrder, ledger: Ledger, sources: list[StopSource] | None = None
) -> FoundStop | None:
    """The first stop a source finds for ``entry``."""
    for source in sources if sources is not None else registered_sources():
        found = source.find(entry, ledger)
        if found is not None and found.price > 0:
            return found
    return None
