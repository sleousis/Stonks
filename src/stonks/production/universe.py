"""The production tick's universe: a fixed ticker list or a stored universe.

``[production].universe`` is either a list of tickers or a universe id
(roadmap 10.5). A list is used as given. A universe id gives its members
on the tick's ``as_of`` date from ``universe_membership``, so refresh the
universe first (the scheduled ``universes_refresh`` job does it daily).

:func:`production_tickers` is what every entrypoint (CLI, API, scheduler)
calls: explicit tickers win, then the configured universe.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

from stonks.lab.universe import resolve, resolve_window

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

TickUniverse = str | Sequence[str]


class EmptyUniverseError(ValueError):
    """The tick has no tickers to rank; the message says what to do."""


def resolve_tick_universe(lake: DuckDBLake, universe: TickUniverse, as_of: date) -> list[str]:
    """Tickers to rank on ``as_of``. A string is a universe id (``KeyError``
    when it has no membership rows); a sequence is a fixed list,
    de-duplicated in order."""
    if isinstance(universe, str):
        return resolve(lake, universe, as_of)
    return list(dict.fromkeys(t for t in universe if t))


def production_tickers(
    lake: DuckDBLake,
    universe: TickUniverse,
    as_of: date,
    *,
    tickers: Sequence[str] | None = None,
) -> list[str]:
    """``tickers`` when given, else the configured ``universe`` on ``as_of``.
    Raises :class:`EmptyUniverseError` when nothing is left."""
    if tickers:
        return list(dict.fromkeys(t for t in tickers if t))
    if isinstance(universe, str):
        try:
            members = resolve_tick_universe(lake, universe, as_of)
        except KeyError:
            raise EmptyUniverseError(
                f"universe {universe!r} has no members yet: refresh it first "
                f"(stonks universe refresh {universe})"
            ) from None
        if not members:
            raise EmptyUniverseError(f"universe {universe!r} has no members on {as_of}")
        return members
    members = resolve_tick_universe(lake, universe, as_of)
    if not members:
        raise EmptyUniverseError(
            "production universe is empty: pass tickers or set [production].universe"
        )
    return members


def window_tickers(lake: DuckDBLake, universe: TickUniverse, start: date, end: date) -> list[str]:
    """Every ticker of ``universe`` on any day of ``start..end`` (a list as
    given). For backtests and sweeps over the configured universe. Raises
    :class:`EmptyUniverseError` when nothing is left."""
    if isinstance(universe, str):
        try:
            members = resolve_window(lake, universe, start, end)
        except KeyError:
            raise EmptyUniverseError(
                f"universe {universe!r} has no members yet: refresh it first "
                f"(stonks universe refresh {universe})"
            ) from None
    else:
        members = list(dict.fromkeys(t for t in universe if t))
    if not members:
        raise EmptyUniverseError("the universe is empty: pass tickers or set [production].universe")
    return members


def universe_id_of(universe: TickUniverse) -> str | None:
    """The universe id when ``universe`` names one, else ``None``."""
    return universe if isinstance(universe, str) else None
