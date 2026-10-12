"""Phase 2: the full daily history of every instrument, through the ingest
pipeline (quality checks, quarantine, adjustment basis). Stocks come
first, then ETFs, then funds, and active names before delisted ones."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from stonks.ingest.backfill.base import (
    BackfillContext,
    BackfillPhase,
    Outcome,
    exchange_of,
    instrument_priority,
    instrument_rows,
    pipeline_outcomes,
)
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec


class DailyPhase(BackfillPhase):
    phase_id = "daily"
    order = 20
    title = "Daily history"
    # a long history is ten thousand bars, so a batch stays small
    batch_size = 8

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        for ticker, _cls, kind, delisted in instrument_rows(ctx):
            yield UnitSpec(ticker, exchange_of(ticker), instrument_priority(kind, delisted))

    def estimate(self, unit: Unit) -> int:
        return 1

    def fetch(self, ctx: BackfillContext, unit: Unit) -> list[Any]:
        return list(ctx.source.fetch_prices(unit.key))

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: rows for unit, rows in fetched}
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_prices(list(results))
        return pipeline_outcomes(result, results, {k: len(v) for k, v in results.items()})
