"""DeFi total value locked per chain from DefiLlama, a free source (no
EODHD budget). Each fetch returns a chain's whole daily series, so each
chain is one unit a day and a rerun only adds the newest days; a unit from
a day that passed is done with nothing fetched."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec
from stonks.ingest.sources.base import DataSourceError

SOURCE = "defillama"


def split_key(key: str) -> tuple[str, date]:
    chain, _, day = key.rpartition(":")
    return chain, date.fromisoformat(day)


class TvlPhase(BackfillPhase):
    phase_id = "tvl"
    order = 72
    title = "DeFi TVL per chain, daily (free)"
    batch_size = 10
    metered = False
    relist_after = timedelta(hours=6)

    def ready(self, ctx: BackfillContext) -> str | None:
        try:
            ctx.source_for(SOURCE)
        except DataSourceError as exc:
            return f"DefiLlama is not set up: {exc}"
        return None

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        chains = ctx.settings.tvl_chains
        if chains is None:
            chains = list(ctx.source_for(SOURCE).list_tvl_chains())
        for rank, chain in enumerate(dict.fromkeys(c.strip().lower() for c in chains if c.strip())):
            # the largest chains first
            yield UnitSpec(f"{chain}:{ctx.today.isoformat()}", None, priority=min(rank // 50, 9))

    def estimate(self, unit: Unit) -> int:
        return 0  # free

    def fetch(self, ctx: BackfillContext, unit: Unit) -> list[Any] | None:
        chain, day = split_key(unit.key)
        if day != ctx.today:
            return None  # today's unit fetches the same series
        return list(ctx.source_for(SOURCE).fetch_chain_tvl(chain))

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, rows in fetched:
            if rows is None:
                out[unit.key] = Outcome(note="a later day fetches it")
                continue
            chain, _ = split_key(unit.key)
            pipeline = ctx.make_pipeline(ReplaySource(SOURCE, {chain: rows}))
            result = pipeline.run_defi_tvl([chain])
            if result.tickers_failed:
                out[unit.key] = Outcome(error=f"ingest run {result.run_id} failed")
            else:
                out[unit.key] = Outcome(rows=len(rows))
        return out
