"""Stock borrow rates from IBKR's public short stock files, a free source
(no EODHD budget). The files hold today only, so each market is one unit a
day; a unit from a day that passed is done with nothing fetched."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec
from stonks.ingest.sources.base import DataSourceError

SOURCE = "ibkr_borrow"


def split_key(key: str) -> tuple[str, date]:
    market, _, day = key.rpartition(":")
    return market, date.fromisoformat(day)


class BorrowPhase(BackfillPhase):
    phase_id = "borrow"
    order = 71
    title = "Stock borrow rates, daily (free)"
    batch_size = 4
    metered = False
    #: listed again a few times a day, so each new day gets its units
    relist_after = timedelta(hours=6)

    def ready(self, ctx: BackfillContext) -> str | None:
        try:
            ctx.source_for(SOURCE)
        except DataSourceError as exc:
            return f"the short stock files are not set up: {exc}"
        return None

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        from stonks.ingest.sources.ibkr_borrow import SHORT_STOCK_MARKETS

        markets = ctx.settings.borrow_markets or list(SHORT_STOCK_MARKETS)
        for market in dict.fromkeys(m.strip().lower() for m in markets if m.strip()):
            yield UnitSpec(f"{market}:{ctx.today.isoformat()}", None, priority=0)

    def estimate(self, unit: Unit) -> int:
        return 0  # free

    def fetch(self, ctx: BackfillContext, unit: Unit) -> list[Any] | None:
        market, day = split_key(unit.key)
        if day != ctx.today:
            return None  # the file now holds another day
        return list(ctx.source_for(SOURCE).fetch_borrow_rates(market))

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, rows in fetched:
            if rows is None:
                out[unit.key] = Outcome(note="the day passed before it was fetched")
                continue
            market, _ = split_key(unit.key)
            pipeline = ctx.make_pipeline(ReplaySource(SOURCE, {market: rows}))
            result = pipeline.run_borrow_rates([market])
            if result.tickers_failed:
                out[unit.key] = Outcome(error=f"ingest run {result.run_id} failed")
            else:
                out[unit.key] = Outcome(rows=len(rows))
        return out
