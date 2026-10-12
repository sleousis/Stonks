"""US Treasury rates: bill rates, the nominal and real par yield curves and
the long-term rates, one year a unit from ``treasury_start_year``, into
``treasury_rates`` with their versions (P12). A year with no data costs
one call and stores nothing. The current year is one unit a month
(``curve:2026@2026-10``), so its newest days come in each month, and the
whole year again as a past year once it ends."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome, pipeline_outcomes
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import Unit, UnitSpec
from stonks.ingest.reference_schemas import TreasuryCurve

#: The curves asked for. ``long_term`` also brings ``long_term_real``.
CURVES: tuple[TreasuryCurve, ...] = ("par_nominal", "bill", "par_real", "long_term")


def split_key(key: str) -> tuple[TreasuryCurve, int]:
    curve, _, year = key.partition("@")[0].rpartition(":")
    return curve, int(year)  # type: ignore[return-value]


def unit_key(curve: TreasuryCurve, year: int, today: date) -> str:
    """``curve:year`` for a past year, plus ``@YYYY-MM`` for the current one."""
    if year < today.year:
        return f"{curve}:{year}"
    return f"{curve}:{year}@{today:%Y-%m}"


class TreasuryPhase(BackfillPhase):
    phase_id = "treasury"
    order = 43
    title = "US Treasury rates"
    batch_size = 10

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        # lists again each run (no network), so a new year shows up
        for year in range(ctx.today.year, ctx.settings.treasury_start_year - 1, -1):
            for curve in CURVES:
                yield UnitSpec(unit_key(curve, year, ctx.today), None, priority=0)

    def estimate(self, unit: Unit) -> int:
        return 1

    def eta_cost(self, settings: BackfillSettings, today: date) -> float:
        return 1.0

    def fetch(self, ctx: BackfillContext, unit: Unit) -> list[Any]:
        return list(ctx.source.fetch_treasury_rates(*split_key(unit.key)))

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        # the pipeline names a unit ``curve:year``, without the month
        names = {unit.key: "{}:{}".format(*split_key(unit.key)) for unit, _ in fetched}
        results = {names[unit.key]: rows for unit, rows in fetched}
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_treasury_rates([split_key(k) for k in results])
        by_name = pipeline_outcomes(result, results, {k: len(v) for k, v in results.items()})
        return {key: by_name[name] for key, name in names.items()}
