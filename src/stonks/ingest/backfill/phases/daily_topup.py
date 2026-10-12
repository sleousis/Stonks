"""Phase 3: top up recent days with one bulk call per exchange and day.

The daily phase runs for days, so a ticker fetched early misses the days
after its fetch. Each weekday since the first ticker of an exchange was
done (at most ``topup_days`` back) is one unit: one bulk call for the
whole exchange, stored for the tickers fetched on or before that day."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome, exchange_of
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec


def _split(key: str) -> tuple[str, date]:
    exchange, _, day = key.rpartition(":")
    return exchange, date.fromisoformat(day)


class DailyTopupPhase(BackfillPhase):
    phase_id = "daily_topup"
    order = 21
    title = "Recent days by bulk call"
    batch_size = 1

    def __init__(self) -> None:
        #: ticker -> the day its daily history was fetched, read when the
        #: phase lists its units (each run, after the daily phase)
        self._done: dict[str, date] | None = None

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        self._done = ctx.state.done_since("daily")
        first_done: dict[str, date] = {}
        for ticker, day in self._done.items():
            exchange = exchange_of(ticker)
            if exchange is not None and (exchange not in first_done or day < first_done[exchange]):
                first_done[exchange] = day
        yesterday = ctx.today - timedelta(days=1)
        oldest = ctx.today - timedelta(days=ctx.settings.topup_days)
        for exchange, start in sorted(first_done.items()):
            day = max(start, oldest)
            while day <= yesterday:
                if day.weekday() < 5:
                    yield UnitSpec(f"{exchange}:{day.isoformat()}", exchange, priority=0)
                day += timedelta(days=1)

    def estimate(self, unit: Unit) -> int:
        return 100

    def fetch(self, ctx: BackfillContext, unit: Unit) -> list[Any]:
        exchange, day = _split(unit.key)
        return ctx.source.fetch_bulk_eod(exchange, day)

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        done = self._done if self._done is not None else ctx.state.done_since("daily")
        out: dict[str, Outcome] = {}
        for unit, rows in fetched:
            day = _split(unit.key)[1]
            by_ticker: dict[str, list[Any]] = defaultdict(list)
            for row in rows:
                finished = done.get(row.ticker)
                # a ticker fetched after this day already holds its bar
                if finished is not None and finished <= day:
                    by_ticker[row.ticker].append(row)
            if not by_ticker:
                out[unit.key] = Outcome(note="no ticker needed this day")
                continue
            pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, by_ticker))
            result = pipeline.run_prices(list(by_ticker), since=day, until=day)
            note = f"{result.tickers_failed} tickers not stored" if result.tickers_failed else None
            out[unit.key] = Outcome(rows=result.tickers_ok, note=note)
        return out
