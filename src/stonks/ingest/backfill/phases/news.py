"""Phase 9: news and news sentiment of active stocks and ETFs."""

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

PARTS: tuple[CompanyPart, ...] = ("news", "sentiment")


class NewsPhase(BackfillPhase):
    phase_id = "news"
    order = 53
    title = "News and sentiment"
    # a page of a thousand articles is several megabytes
    batch_size = 4

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        rows = instrument_rows(
            ctx, security_types=["common_stock", "adr", "etf"], include_delisted=False
        )
        for ticker, _cls, kind, delisted in rows:
            yield UnitSpec(ticker, exchange_of(ticker), instrument_priority(kind, delisted))

    def estimate(self, unit: Unit) -> int:
        return 10

    def fetch(self, ctx: BackfillContext, unit: Unit) -> Any:
        return ctx.source.fetch_company_data(unit.key, PARTS)

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: data for unit, data in fetched}
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_company_data(list(results), PARTS)
        rows = {
            k: len(d.metadata.news) + len(d.metadata.news_sentiment) for k, d in results.items()
        }
        return pipeline_outcomes(result, results, rows)
