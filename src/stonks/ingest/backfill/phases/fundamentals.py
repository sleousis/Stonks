"""Phase 5: fundamentals of every common stock, ETF and fund.

One ``/fundamentals`` response per ticker fills the profile and everything
that rides with it (analysts, holders, officers, shares outstanding, ESG,
earnings history) plus the statements with their versions (stocks) or the
fund holdings (ETFs and funds). Stocks also get market cap history (active
ones unless ``market_cap_delisted``) and insider transactions on the
``insider_exchanges``."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from stonks.ingest.backfill.base import (
    BackfillContext,
    BackfillPhase,
    Outcome,
    exchange_of,
    instrument_priority,
    instrument_rows,
    pipeline_outcomes,
)
from stonks.ingest.backfill.replay import ReplaySource
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import Unit, UnitSpec
from stonks.ingest.metadata_bundle import CompanyData, CompanyPart

_STOCKS = ("common_stock", "adr")
_FUNDS = ("etf", "fund")
#: API calls per part (the profile, statements and holdings share one call).
_COSTS: dict[str, int] = {"fundamentals": 10, "market_cap": 10, "insiders": 10}


def parts_for(
    kind: str | None, exchange: str | None, delisted: bool, settings: BackfillSettings
) -> tuple[CompanyPart, ...]:
    """The parts one ticker of ``kind`` gets."""
    if kind in _FUNDS:
        return ("profile", "fund_holdings")
    parts: list[CompanyPart] = ["profile", "statements"]
    if not delisted or settings.market_cap_delisted:
        parts.append("market_cap")
    if exchange in {e.upper() for e in settings.insider_exchanges}:
        parts.append("insiders")
    return tuple(parts)


def _cost(parts: Sequence[CompanyPart]) -> int:
    calls = _COSTS["fundamentals"]
    calls += sum(_COSTS[p] for p in parts if p in ("market_cap", "insiders"))
    return calls


def _rows(data: CompanyData) -> int:
    meta = data.metadata
    rows = len(meta.shares_outstanding) + len(meta.analyst_forecasts)
    rows += len(meta.market_cap_history) + len(meta.insider_transactions) + len(data.fund_holdings)
    if data.statements is not None:
        st = data.statements
        rows += len(st.income) + len(st.balance) + len(st.cashflow)
    return rows + (1 if meta.profile is not None else 0)


class FundamentalsPhase(BackfillPhase):
    phase_id = "fundamentals"
    order = 40
    title = "Fundamentals, profiles and holdings"
    batch_size = 5

    def __init__(self) -> None:
        #: ticker -> parts, filled when the phase lists its units (each run)
        self._parts: dict[str, tuple[CompanyPart, ...]] = {}

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        rows = instrument_rows(ctx, security_types=_STOCKS + _FUNDS)
        self._parts = {}
        for ticker, _cls, kind, delisted in rows:
            exchange = exchange_of(ticker)
            self._parts[ticker] = parts_for(kind, exchange, delisted, ctx.settings)
            yield UnitSpec(ticker, exchange, instrument_priority(kind, delisted))

    def _unit_parts(self, unit: Unit) -> tuple[CompanyPart, ...]:
        return self._parts.get(unit.key, ("profile", "statements"))

    def estimate(self, unit: Unit) -> int:
        return _cost(self._unit_parts(unit))

    def fetch(self, ctx: BackfillContext, unit: Unit) -> CompanyData:
        return ctx.source.fetch_company_data(unit.key, self._unit_parts(unit))

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        results = {unit.key: data for unit, data in fetched}
        parts = sorted({p for unit, _ in fetched for p in self._unit_parts(unit)})
        pipeline = ctx.make_pipeline(ReplaySource(ctx.source.source_id, results))
        result = pipeline.run_company_data(list(results), parts)  # type: ignore[arg-type]
        return pipeline_outcomes(result, results, {k: _rows(d) for k, d in results.items()})
