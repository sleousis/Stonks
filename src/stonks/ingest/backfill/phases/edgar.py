"""SEC EDGAR, a free source with its own rate limit (no EODHD budget):
every filing and the Form 4 insider trades of each active US stock, plus
the 13F holdings of the managers in ``edgar_filers``, each with its
acceptance time as ``known_at``.

The SEC asks every client to name itself, so the phase skips with a clear
message until ``[sources.edgar] user_agent`` (or
``STONKS_EDGAR_USER_AGENT``) holds a name and a contact address. A ticker
EDGAR does not list is done with nothing stored."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from stonks.ingest.backfill.base import (
    BackfillContext,
    BackfillPhase,
    Outcome,
    instrument_priority,
    instrument_rows,
)
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.state import Unit, UnitSpec
from stonks.ingest.sources.base import DataSourceError

SOURCE = "edgar"
#: Unit keys of 13F managers start with this.
FILER = "13F:"
_KINDS = ("common_stock", "adr", "preferred_stock")


class EdgarPhase(BackfillPhase):
    phase_id = "edgar"
    order = 70
    title = "SEC filings, insider trades and 13F holdings (free)"
    batch_size = 4
    metered = False

    def ready(self, ctx: BackfillContext) -> str | None:
        try:
            ctx.source_for(SOURCE)
        except DataSourceError as exc:
            return f"SEC EDGAR is not set up: {exc}"
        return None

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        # EDGAR's ticker list names current tickers only
        rows = instrument_rows(ctx, security_types=_KINDS, exchanges=["US"], include_delisted=False)
        for ticker, _cls, kind, delisted in rows:
            yield UnitSpec(ticker, "US", instrument_priority(kind, delisted))
        for cik in ctx.settings.edgar_filers:
            digits = cik.strip()
            if digits.isdigit():
                yield UnitSpec(f"{FILER}{int(digits)}", None, priority=0)

    def estimate(self, unit: Unit) -> int:
        return 0  # free

    def fetch(self, ctx: BackfillContext, unit: Unit) -> dict[str, list[Any]]:
        source = ctx.source_for(SOURCE)
        if unit.key.startswith(FILER):
            cik = unit.key[len(FILER) :]
            return {f"holdings:{cik}": list(source.fetch_institutional_holdings(cik))}
        out: dict[str, list[Any]] = {}
        kinds = set(ctx.settings.edgar_kinds)
        if "filings" in kinds:
            out[f"filings:{unit.key}"] = list(source.fetch_filings(unit.key))
        if "insiders" in kinds:
            out[f"insiders:{unit.key}"] = list(source.fetch_insider_filings(unit.key))
        return out

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for unit, results in fetched:
            pipeline = ctx.make_pipeline(ReplaySource(SOURCE, results))
            failed: list[int] = []
            for key in results:
                kind, _, name = key.partition(":")
                if kind == "holdings":
                    result = pipeline.run_institutional_holdings([name])
                elif kind == "filings":
                    result = pipeline.run_filings([name])
                else:
                    result = pipeline.run_insider_filings([name])
                if result.tickers_failed:
                    failed.append(result.run_id)
            rows = sum(len(v) for v in results.values())
            if failed:
                out[unit.key] = Outcome(error=f"ingest runs {failed} could not store it")
            else:
                out[unit.key] = Outcome(rows=rows)
        return out
