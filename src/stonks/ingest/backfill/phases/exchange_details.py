"""Exchange details: trading hours, time zone and holidays (from
``exchange_holidays_start`` to a few years ahead) of every exchange the
source lists, into ``exchange_details`` and ``exchange_holidays``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome, pipeline_outcomes
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec

_FIRST = ("US", "LSE", "XETRA", "PA", "TO")


def holiday_window(ctx: BackfillContext) -> tuple[date, date]:
    settings = ctx.settings
    end = date(ctx.today.year + settings.exchange_holidays_years_ahead, 12, 31)
    return settings.exchange_holidays_start, end


class ExchangeDetailsPhase(BackfillPhase):
    phase_id = "exchange_details"
    order = 42
    title = "Exchange hours and holidays"
    batch_size = 10

    def listing_ttl(self, ctx: BackfillContext) -> timedelta | None:
        return timedelta(days=ctx.settings.symbols_relist_days)

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        skip = {e.upper() for e in ctx.settings.skip_exchanges}
        codes = [e.code.upper() for e in ctx.source.list_exchanges()]
        codes += [e.upper() for e in ctx.settings.extra_exchanges]
        for code in dict.fromkeys(codes):
            if code not in skip:
                yield UnitSpec(code, code, priority=0 if code in _FIRST else 1)

    def estimate(self, unit: Unit) -> int:
        return 1

    def fetch(self, ctx: BackfillContext, unit: Unit) -> Any:
        start, end = holiday_window(ctx)
        return ctx.source.fetch_exchange_details(unit.key, start, end)

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: row for unit, row in fetched}
        start, end = holiday_window(ctx)
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_exchange_details(list(results), start, end)
        rows = {k: 1 + len(v.holidays) for k, v in results.items()}
        return pipeline_outcomes(result, results, rows)
