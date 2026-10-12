"""Index members and their history: for every index the source lists, the
members today and every dated join and exit, into
``index_constituent_snapshots`` and ``index_constituent_changes``. A stored
index universe then rebuilds membership on any past day, delisted names
included (survivorship free, docs/principles.md)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome, pipeline_outcomes
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec

#: The source's exchange code for indices.
INDICES = "INDX"
#: Indices whose history comes first.
_FIRST = ("GSPC", "NDX", "DJI", "RUT", "FTSE", "GDAXI", "FCHI", "STOXX50E", "N225", "HSI")


class IndexConstituentsPhase(BackfillPhase):
    phase_id = "index_constituents"
    order = 41
    title = "Index members and history"
    batch_size = 10

    def listing_ttl(self, ctx: BackfillContext) -> timedelta | None:
        return timedelta(days=ctx.settings.symbols_relist_days)

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        codes = ctx.settings.index_codes
        if codes is None:
            codes = [
                s.ticker.rpartition(".")[0] or s.ticker for s in ctx.source.list_symbols(INDICES)
            ]
        first = {c: i for i, c in enumerate(_FIRST)}
        for code in dict.fromkeys(c.strip().upper() for c in codes if c.strip()):
            # market wide: --exchanges never filters it out
            yield UnitSpec(code, None, priority=0 if code in first else 1)

    def estimate(self, unit: Unit) -> int:
        return 10  # one /fundamentals call

    def fetch(self, ctx: BackfillContext, unit: Unit) -> Any:
        return ctx.source.fetch_index_components(unit.key, ctx.today)

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: row for unit, row in fetched}
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_index_components(list(results), ctx.today)
        rows = {k: len(v.members) + len(v.changes) for k, v in results.items()}
        return pipeline_outcomes(result, results, rows)
