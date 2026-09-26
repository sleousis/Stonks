"""Refresh: materialise a stored universe into ``universe_membership``.

The provider of the definition's kind computes the spans (reads only).
Then one lake transaction writes any instrument rows or index history it
learned, replaces the universe's membership rows with the spans and
stamps the definition. Refreshing twice with the same inputs leaves the
same rows, so a scheduled refresh is safe to repeat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

from stonks.ingest.pipeline import _rows_to_df
from stonks.logging import get_logger
from stonks.universes.base import IndexSourceFactory, RefreshContext, SourceFactory
from stonks.universes.registry import build_index_source, provider_for
from stonks.universes.store import UniverseStore

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.universes.refresh")


@dataclass
class RefreshResult:
    universe_id: str
    kind: str
    #: Distinct tickers with at least one span.
    members: int
    #: Tickers with an open span (members on the refresh date).
    current_members: int
    spans: int
    refreshed_at: datetime | None
    warnings: list[str] = field(default_factory=list)


def refresh_universe(
    lake: DuckDBLake,
    universe_id: str,
    *,
    as_of: date | None = None,
    source_factory: SourceFactory | None = None,
    index_source_factory: IndexSourceFactory | None = None,
) -> RefreshResult:
    """Materialise ``universe_id`` (``KeyError`` when unknown,
    ``ValueError`` when its provider cannot). ``source_factory`` builds a
    data source by id for exchange universes; ``index_source_factory``
    builds an index adapter by id (default: the index source registry)."""
    store = UniverseStore(lake)
    definition = store.get(universe_id)
    provider = provider_for(definition.kind)
    spec = provider.parse_spec(definition.spec)
    ctx = RefreshContext(
        lake=lake,
        as_of=as_of or date.today(),
        source=source_factory,
        index_source=index_source_factory or build_index_source,
    )
    out = provider.materialize(spec, ctx)
    with lake.transaction():
        if out.profiles:
            lake.upsert_instrument_profile(_rows_to_df(out.profiles))
        if out.index_history is not None:
            store.save_index_history(out.index_history)
        n_spans = store.replace_membership(universe_id, out.spans)
        tickers = {s.ticker for s in out.spans}
        current = {s.ticker for s in out.spans if s.end_date is None or s.end_date > ctx.as_of}
        store.mark_refreshed(universe_id, len(tickers))
    for warning in out.warnings:
        _log.warning("universe.refresh.warning", universe_id=universe_id, warning=warning)
    _log.info(
        "universe.refreshed",
        universe_id=universe_id,
        kind=definition.kind,
        members=len(tickers),
        spans=n_spans,
    )
    return RefreshResult(
        universe_id=universe_id,
        kind=definition.kind,
        members=len(tickers),
        current_members=len(current),
        spans=n_spans,
        refreshed_at=store.get(universe_id).refreshed_at,
        warnings=list(out.warnings),
    )
