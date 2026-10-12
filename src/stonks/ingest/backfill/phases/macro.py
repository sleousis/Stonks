"""Phase 7: every macro indicator the source lists, for every country."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.countries import MACRO_COUNTRIES
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec

#: Countries whose series come first.
_FIRST = ("USA",)


class MacroPhase(BackfillPhase):
    phase_id = "macro"
    order = 51
    title = "Macro indicators"
    batch_size = 10

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        settings = ctx.settings
        countries = settings.macro_countries or list(MACRO_COUNTRIES)
        indicators = settings.macro_indicators or list(ctx.source.list_macro_indicators())
        for country in countries:
            for indicator in indicators:
                priority = 0 if country.upper() in _FIRST else 1
                yield UnitSpec(f"{country.upper()}:{indicator}", priority=priority)

    def estimate(self, unit: Unit) -> int:
        return 10

    def fetch(self, ctx: BackfillContext, unit: Unit) -> list[Any]:
        country, _, indicator = unit.key.partition(":")
        return list(ctx.source.fetch_macro_indicator(country, indicator))

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, rows in fetched:
            country, _, indicator = unit.key.partition(":")
            pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, {unit.key: rows}))
            result = pipeline.run_macro_indicators([country], [indicator])
            if result.tickers_failed:
                out[unit.key] = Outcome(error=f"ingest run {result.run_id} failed")
            else:
                out[unit.key] = Outcome(rows=len(rows))
        return out
