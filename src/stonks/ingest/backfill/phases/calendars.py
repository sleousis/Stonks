"""Phase 6: the earnings and dividend calendars and economic events, from
``calendar_start`` to today, one month a step (each calendar is one unit
with a cursor, so a stop resumes at the month it reached)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import Unit, UnitSpec
from stonks.ingest.pipeline import CALENDAR_KINDS, CalendarKind

#: Rough calls per month: dividends page through the whole market, and
#: economic events are asked a week at a time.
_COSTS: dict[str, int] = {"earnings": 1, "dividends": 5, "economic": 6}


def month_window(start: date, today: date) -> tuple[date, date]:
    """``start`` to the end of its month, never past ``today``."""
    next_month = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
    return start, min(next_month - timedelta(days=1), today)


class CalendarsPhase(BackfillPhase):
    phase_id = "calendars"
    order = 50
    title = "Event calendars"
    batch_size = 3

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        for kind in CALENDAR_KINDS:
            yield UnitSpec(kind)

    def estimate(self, unit: Unit) -> int:
        return _COSTS.get(unit.key, 1)

    def eta_cost(self, settings: BackfillSettings, today: date) -> float:
        months = (today.year - settings.calendar_start.year) * 12 + today.month
        months -= settings.calendar_start.month - 1
        return float(max(1, months) * sum(_COSTS.values()) / len(_COSTS))

    def _start(self, ctx: BackfillContext, unit: Unit) -> date:
        return date.fromisoformat(unit.cursor) if unit.cursor else ctx.settings.calendar_start

    def fetch(self, ctx: BackfillContext, unit: Unit) -> tuple[date, date, list[Any]]:
        start, end = month_window(self._start(ctx, unit), ctx.today)
        src = ctx.source
        if unit.key == "earnings":
            rows = list(src.fetch_earnings_calendar(start, end, None))
        elif unit.key == "dividends":
            rows = list(src.fetch_dividend_calendar(start, end, None))
        else:
            rows = list(src.fetch_economic_events(start, end, None))
        return start, end, rows

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, (start, end, rows) in fetched:
            kind: CalendarKind = unit.key  # type: ignore[assignment]
            pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, {kind: rows}))
            result = pipeline.run_calendars(start, end, kinds=[kind])
            if result.tickers_failed:
                out[unit.key] = Outcome(error=f"ingest run {result.run_id} failed")
            elif end >= ctx.today:
                out[unit.key] = Outcome(rows=len(rows))
            else:
                nxt = (end + timedelta(days=1)).isoformat()
                out[unit.key] = Outcome(rows=len(rows), finished=False, cursor=nxt)
        return out
