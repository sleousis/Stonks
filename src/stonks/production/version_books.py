"""Model version books (roadmap 22.6): a candidate model runs as a model
book before it can trade.

While a strategy has a candidate version (``registry.versions``), the tick
keeps one model book per candidate and one for the live version, all in
``model_version_decisions`` and ``model_version_snapshots``, keyed by
strategy and version. The books work exactly like the shadow model books
(``production.shadow``: virtual portfolio, same risk policy, an in-memory
simulated broker, never the real ledger). The live version's book starts
the day the first candidate appears, so the swap check compares the two
models over the same days, from the same cash.

Book ids are ``<strategy>@v<n>``. A strategy with no candidate keeps no
version book, and a retired strategy never does.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial

from stonks.core.protocols import Strategy
from stonks.production.prices import held_tickers
from stonks.production.shadow import BookStore
from stonks.registry.store import StrategyRegistry
from stonks.registry.versions import ModelVersionRegistry, book_id
from stonks.store.state import SqliteState


def versions_recorded(state: SqliteState) -> bool:
    """The state has the model version tables (migration 029)."""
    return bool(
        state.sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_versions'")
    )


def version_book(strategy_id: str, version: int) -> BookStore:
    """The model book of one version of a strategy."""
    return BookStore(
        book_id=book_id(strategy_id, version),
        decisions="model_version_decisions",
        snapshots="model_version_snapshots",
        key=(("strategy_id", strategy_id), ("version", int(version))),
    )


@dataclass(frozen=True)
class VersionBook:
    strategy_id: str
    version: int
    store: BookStore
    load: Callable[[], Strategy]


def active_version_books(state: SqliteState, registry: StrategyRegistry) -> list[VersionBook]:
    """Every book the tick advances today: each candidate version of a
    strategy that is not retired, and that strategy's live version."""
    if not versions_recorded(state):
        return []
    versions = ModelVersionRegistry.on(registry)
    candidates = versions.candidates()
    books: list[VersionBook] = []
    seen: set[str] = set()
    for candidate in candidates:
        sid = candidate.strategy_id
        if sid not in seen:
            seen.add(sid)
            live = versions.live(sid)
            books.append(_book(versions, sid, live.version))
        books.append(_book(versions, sid, candidate.version))
    return books


def _book(versions: ModelVersionRegistry, strategy_id: str, version: int) -> VersionBook:
    return VersionBook(
        strategy_id=strategy_id,
        version=version,
        store=version_book(strategy_id, version),
        load=partial(versions.load, strategy_id, version),
    )


def version_held_tickers(state: SqliteState, books: Sequence[VersionBook]) -> list[str]:
    """Every ticker held in the latest virtual portfolio of ``books``."""
    held: set[str] = set()
    for book in books:
        portfolio, _ = book.store.load(state, 0.0)
        held.update(held_tickers(portfolio.positions))
    return sorted(held)


def version_book_curve(
    state: SqliteState, strategy_id: str, version: int, *, since: date | None = None
) -> list[tuple[date, float]]:
    """``(as_of, total_value)`` of one version book, from ``since`` on."""
    curve = version_book(strategy_id, version).curve(state)
    return [(d, v) for d, v in curve if since is None or d >= since]
