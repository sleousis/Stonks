"""Phase 1: every exchange and its symbol list, active and delisted, stored
as instruments with their asset class, equity kind, ISIN and delisted flag.
A profile field the listing leaves empty keeps what the lake already has."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import timedelta
from typing import Any

import pandas as pd

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.state import Unit, UnitSpec

#: Profiles per lake write.
_CHUNK = 5000
#: Exchanges listed first (the rest follow by code).
_FIRST = ("US",)


class SymbolsPhase(BackfillPhase):
    phase_id = "symbols"
    order = 10
    title = "Exchanges and symbol lists"
    batch_size = 2

    def listing_ttl(self, ctx: BackfillContext) -> timedelta | None:
        return timedelta(days=ctx.settings.symbols_relist_days)

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        skip = {e.upper() for e in ctx.settings.skip_exchanges}
        codes = [e.code.upper() for e in ctx.source.list_exchanges()]
        codes += [e.upper() for e in ctx.settings.extra_exchanges]
        for code in dict.fromkeys(codes):
            if code in skip:
                continue
            yield UnitSpec(key=code, exchange=code, priority=0 if code in _FIRST else 1)

    def estimate(self, unit: Unit) -> int:
        return 2  # the active list and the delisted list

    def fetch(self, ctx: BackfillContext, unit: Unit) -> pd.DataFrame:
        listings = ctx.source.list_symbols(unit.key)
        return pd.DataFrame(
            [
                {
                    "id": s.ticker,
                    "asset_class": s.asset_class,
                    "exchange": s.exchange,
                    "currency": s.currency,
                    "name": s.name,
                    "is_delisted": s.is_delisted,
                    "security_type": s.security_type,
                    "isin": s.isin,
                }
                for s in listings
            ]
        )

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, frame in fetched:
            for start in range(0, len(frame), _CHUNK):
                with ctx.lake.transaction():
                    ctx.lake.upsert_instrument_profile(frame.iloc[start : start + _CHUNK])
            out[unit.key] = Outcome(rows=len(frame))
        return out
