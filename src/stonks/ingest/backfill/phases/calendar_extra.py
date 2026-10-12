"""The IPO and split calendars for the whole market, from
``calendar_start`` to today, one month a step (each calendar is one unit
with a cursor), into ``ipo_calendar`` and ``split_calendar``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.phases.calendars import month_window
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import Unit, UnitSpec
from stonks.ingest.pipeline import LISTING_CALENDAR_KINDS, ListingCalendarKind


class CalendarExtraPhase(BackfillPhase):
    phase_id = "calendar_extra"
    order = 44
    title = "IPO and split calendars"
    batch_size = 2

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        for kind in LISTING_CALENDAR_KINDS:
            yield UnitSpec(kind)

    def estimate(self, unit: Unit) -> int:
        return 1

    def eta_cost(self, settings: BackfillSettings, today: date) -> float:
        months = (today.year - settings.calendar_start.year) * 12 + today.month
        months -= settings.calendar_start.month - 1
        return float(max(1, months))

    def fetch(self, ctx: BackfillContext, unit: Unit) -> tuple[date, date, list[Any]]:
        begin = date.fromisoformat(unit.cursor) if unit.cursor else ctx.settings.calendar_start
        start, end = month_window(begin, ctx.today)
        if unit.key == "ipos":
            rows = list(ctx.source.fetch_ipo_calendar(start, end))
        else:
            rows = list(ctx.source.fetch_split_calendar(start, end))
        return start, end, rows

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, (start, end, rows) in fetched:
            kind: ListingCalendarKind = unit.key  # type: ignore[assignment]
            pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, {kind: rows}))
            result = pipeline.run_listing_calendars(start, end, kinds=[kind])
            if result.tickers_failed:
                out[unit.key] = Outcome(error=f"ingest run {result.run_id} failed")
            elif end >= ctx.today:
                out[unit.key] = Outcome(rows=len(rows))
            else:
                nxt = (end + timedelta(days=1)).isoformat()
                out[unit.key] = Outcome(rows=len(rows), finished=False, cursor=nxt)
        return out
