"""Phase 8: the full history of every currency pair the source lists on
its forex exchange, into ``fx_rates``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec

#: The source's exchange code for currency pairs.
FOREX = "FOREX"


def pair_of(code: str) -> tuple[str, str] | None:
    """``EURUSD`` as ``("EUR", "USD")``; anything but six letters is not a pair."""
    code = code.upper()
    if len(code) != 6 or not code.isascii() or not code.isalpha():
        return None
    return code[:3], code[3:]


class FxPhase(BackfillPhase):
    phase_id = "fx"
    order = 52
    title = "Currency pairs"
    batch_size = 20

    def listing_ttl(self, ctx: BackfillContext) -> timedelta | None:
        return timedelta(days=ctx.settings.symbols_relist_days)

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        seen: set[str] = set()
        for listing in ctx.source.list_symbols(FOREX):
            code = listing.ticker.rpartition(".")[0] or listing.ticker
            pair = pair_of(code)
            if pair is None or code.upper() in seen:
                continue
            seen.add(code.upper())
            yield UnitSpec(code.upper(), FOREX, priority=1 if listing.is_delisted else 0)

    def estimate(self, unit: Unit) -> int:
        return 1

    def fetch(self, ctx: BackfillContext, unit: Unit) -> list[Any]:
        pair = pair_of(unit.key)
        if pair is None:
            return []
        return list(ctx.source.fetch_fx_rates(*pair))

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: rows for unit, rows in fetched}
        pairs = [p for p in (pair_of(k) for k in results) if p is not None]
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_fx_rates(pairs)
        failed = set(result.failed)
        error = f"the ingest pipeline could not store it (ingest run {result.run_id})"
        # the pipeline names a failed pair by its log context, which holds the code
        return {
            k: Outcome(error=error)
            if any(k in f for f in failed)
            else Outcome(rows=len(results[k]))
            for k in results
        }
