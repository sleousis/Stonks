"""The production tick's universe: a fixed ticker list or a stored universe.

:func:`resolve_tick_universe` is what the tick will call once its
settings accept a universe id (roadmap 10.5, integration step). A list is
returned as given; a universe id returns its members on the tick's
``as_of`` date from ``universe_membership`` (refresh the universe first,
for example with the scheduled refresh job).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

from stonks.lab.universe import resolve

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

TickUniverse = str | Sequence[str]


def resolve_tick_universe(lake: DuckDBLake, universe: TickUniverse, as_of: date) -> list[str]:
    """Tickers to rank on ``as_of``. A string is a universe id (``KeyError``
    when it has no membership rows); a sequence is a fixed list,
    de-duplicated in order."""
    if isinstance(universe, str):
        return resolve(lake, universe, as_of)
    return list(dict.fromkeys(t for t in universe if t))
