"""Identifiers: ISIN, CUSIP, CIK, FIGI and LEI for every ticker, one page
of an exchange a step (each exchange is one unit with the next offset as
its cursor), onto the instruments the lake already has. A NULL never
clears a stored identifier."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from stonks.ingest.backfill.base import (
    BackfillContext,
    BackfillPhase,
    Outcome,
)
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import Unit, UnitSpec

#: Rows per page (the vendor's maximum).
PAGE = 1000
_FIRST = ("US",)


class IdMappingPhase(BackfillPhase):
    phase_id = "id_mapping"
    order = 45
    title = "Identifiers (ISIN, CUSIP, CIK, FIGI, LEI)"
    batch_size = 4

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        codes = ctx.settings.id_exchanges
        if codes is None:
            # every exchange with instruments (lists again each run, no
            # network); the lake finds the codes, so no id list is loaded
            rows = ctx.lake.con.execute(
                r"SELECT DISTINCT upper(regexp_extract(id, '^.+\.([^.]+)$', 1)) AS ex "
                "FROM instruments"
            ).fetchall()
            codes = sorted(str(e) for (e,) in rows if e)
        skip = {e.upper() for e in ctx.settings.skip_exchanges}
        for code in dict.fromkeys(c.upper() for c in codes):
            if code not in skip:
                yield UnitSpec(code, code, priority=0 if code in _FIRST else 1)

    def estimate(self, unit: Unit) -> int:
        return 1

    def eta_cost(self, settings: BackfillSettings, today: Any) -> float:
        return 10.0  # pages per exchange vary: US holds about 110 of them

    def fetch(self, ctx: BackfillContext, unit: Unit) -> tuple[int, Any]:
        offset = int(unit.cursor) if unit.cursor else 0
        return offset, ctx.source.fetch_identifiers(unit.key, offset, PAGE)

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, (offset, page) in fetched:
            replay = ReplaySource(ctx.source.source_id, {f"{unit.key}:{offset}": page})
            result = ctx.make_pipeline(replay).run_identifiers(unit.key, offset, PAGE)
            if result.tickers_failed:
                out[unit.key] = Outcome(error=f"ingest run {result.run_id} failed")
                continue
            nxt = offset + page.size
            if page.size == 0 or nxt >= page.total:
                out[unit.key] = Outcome(rows=len(page.rows))
            else:
                out[unit.key] = Outcome(rows=len(page.rows), finished=False, cursor=str(nxt))
        return out
