"""What the intraday phases share: one unit per ticker that walks from the
interval's first day to today, one request window a step, with a cursor so
a stop resumes at the next window. A ticker with nothing in a window jumps
to its first daily bar, so names listed late do not walk empty years."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any, ClassVar

from stonks.core.interval import Interval
from stonks.ingest.backfill.base import (
    BackfillContext,
    BackfillPhase,
    Outcome,
    exchange_of,
    instrument_priority,
    instrument_rows,
)
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import Unit, UnitSpec

_KINDS = ("common_stock", "adr", "etf")


class IntradayPhase(BackfillPhase):
    #: The interval code, a key of ``intraday_start`` and ``intraday_window_days``.
    interval_code: ClassVar[str]

    @property
    def interval(self) -> Interval:
        return Interval.parse(self.interval_code)

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        settings = ctx.settings
        rows = instrument_rows(
            ctx,
            security_types=_KINDS,
            exchanges=settings.intraday_exchanges,
            include_delisted=settings.intraday_delisted,
        )
        for ticker, _cls, kind, delisted in rows:
            yield UnitSpec(ticker, exchange_of(ticker), instrument_priority(kind, delisted))

    def estimate(self, unit: Unit) -> int:
        return 5

    def eta_cost(self, settings: BackfillSettings, today: date) -> float:
        start = settings.intraday_start[self.interval_code]
        days = settings.intraday_window_days[self.interval_code]
        steps = max(1, -(-((today - start).days + 1) // days))
        return 5.0 * steps

    def window(self, ctx: BackfillContext, unit: Unit) -> tuple[date, date]:
        settings = ctx.settings
        start = (
            date.fromisoformat(unit.cursor)
            if unit.cursor
            else settings.intraday_start[self.interval_code]
        )
        days = settings.intraday_window_days[self.interval_code]
        return start, min(start + timedelta(days=days - 1), ctx.today)

    def fetch(self, ctx: BackfillContext, unit: Unit) -> tuple[date, date, list[Any]]:
        start, end = self.window(ctx, unit)
        if start > end:
            return start, end, []
        rows = list(ctx.source.fetch_intraday_bars(unit.key, self.interval, start, end))
        return start, end, rows

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: rows for unit, (_s, _e, rows) in fetched}
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_intraday_bars(list(results), self.interval)
        failed = set(result.failed)
        out: dict[str, Outcome] = {}
        for unit, (_start, end, rows) in fetched:
            if unit.key in failed:
                out[unit.key] = Outcome(error=f"ingest run {result.run_id} failed")
                continue
            if end >= ctx.today:
                out[unit.key] = Outcome(rows=len(rows))
                continue
            nxt = end + timedelta(days=1)
            if not rows:
                first = _first_daily_bar(ctx, unit.key)
                if first is not None and first > nxt:
                    nxt = first
            if nxt > ctx.today:
                out[unit.key] = Outcome(rows=len(rows))
            else:
                out[unit.key] = Outcome(rows=len(rows), finished=False, cursor=nxt.isoformat())
        return out


def _first_daily_bar(ctx: BackfillContext, ticker: str) -> date | None:
    span = ctx.lake.con.execute(
        "SELECT MIN(timestamp) FROM bars WHERE ticker = ? AND interval = '1d'", [ticker]
    ).fetchone()
    value = span[0] if span else None
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value
