"""Phase 4: dividends and splits of every equity (stocks, ETFs, funds)."""

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
from stonks.ingest.metadata_bundle import CompanyPart

PARTS: tuple[CompanyPart, ...] = ("dividends", "splits")


class ActionsPhase(BackfillPhase):
    phase_id = "actions"
    order = 30
    title = "Dividends and splits"
    batch_size = 20

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        for ticker, _cls, kind, delisted in instrument_rows(ctx, asset_classes=["equity"]):
            yield UnitSpec(ticker, exchange_of(ticker), instrument_priority(kind, delisted))

    def estimate(self, unit: Unit) -> int:
        return 2

    def fetch(self, ctx: BackfillContext, unit: Unit) -> Any:
        return ctx.source.fetch_company_data(unit.key, PARTS)

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: data for unit, data in fetched}
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_company_data(list(results), PARTS)
        rows = {k: len(d.metadata.dividends) + len(d.metadata.splits) for k, d in results.items()}
        return pipeline_outcomes(result, results, rows)
