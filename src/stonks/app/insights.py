"""InsightsService (roadmap 15.4): allocation, exposure, P&L over periods,
risk and strategy agreement for one portfolio the caller owns.

It reads the portfolio's latest snapshot whatever wrote it: a tick's own
ledger or a broker sync (``source = 'sync'``). For a synced account the
holdings come from ``broker_positions``, so positions no ticker maps to
still show (as "not covered") with the broker's own value. Callers resolve
``portfolio_id`` through ``PortfolioService.resolve`` first; admins get
:meth:`InsightsService.totals`, sums across every book with no tickers.

The analysis itself is :mod:`stonks.insights`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from stonks.app.context import AppContext
from stonks.app.portfolio import DEFAULT_CURRENCY, PortfolioService
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.insights import (
    AllocationSlice,
    Book,
    Concentration,
    Exposure,
    Holding,
    HoldingAgreement,
    PeriodPnl,
    RiskStats,
    allocation,
    beta,
    concentration,
    exposure,
    period_pnl,
    realized_risk,
    returns_risk,
    strategy_agreement,
    weighted_returns,
)
from stonks.logging import get_logger
from stonks.production.ledger import ledger_filter
from stonks.production.pnl import load_pnl

_log = get_logger("stonks.app.insights")

#: Beta is measured against this unless the caller names another.
DEFAULT_BENCHMARK = "SPY.US"
#: Calendar days of price history behind beta and holdings risk (about a year).
LOOKBACK_DAYS = 365

Source = Literal["tick", "sync"]


class AllocationView(BaseModel):
    asset_class: list[AllocationSlice]
    sector: list[AllocationSlice]
    currency: list[AllocationSlice]
    ticker: list[AllocationSlice]


class RiskView(BaseModel):
    history: RiskStats | None = Field(
        description="From the portfolio's own daily values; null with under three days."
    )
    holdings: RiskStats | None = Field(
        description="Today's weights applied to the last year of price returns; null "
        "without enough prices."
    )
    concentration: Concentration
    returns_as_of: date | None = Field(description="Last price day behind holdings risk and beta.")


class InsightsView(BaseModel):
    portfolio_id: str
    taken_at: datetime | None = Field(description="When the snapshot read was taken.")
    source: Source | None = Field(description="tick (the Stonks ledger) or sync (a broker).")
    currency: str = Field(description="Reporting currency. Amounts are not FX-converted.")
    cash: float
    total_value: float
    allocation: AllocationView
    exposure: Exposure
    pnl: list[PeriodPnl]
    risk: RiskView
    uncovered: list[str] = Field(description="Broker symbols no ticker maps to.")
    unpriced: list[str] = Field(description="Holdings without a price, left out of the numbers.")
    notes: list[str]


class AgreementView(BaseModel):
    portfolio_id: str
    as_of: date | None = Field(description="The price day the strategies scored.")
    strategies: list[str] = Field(description="Active strategies asked, in registry order.")
    skipped: list[str] = Field(description="Active strategies that could not be loaded.")
    holdings: list[HoldingAgreement]


class InsightsTotalsView(BaseModel):
    """Sums over every active portfolio's latest snapshot, for admins. No
    tickers, sectors or per-person numbers (decision 2026-09-26)."""

    portfolios: int
    owners: int
    cash: float
    total_value: float
    asset_class: list[AllocationSlice]
    exposure: Exposure


@dataclass(frozen=True)
class _Loaded:
    book: Book
    taken_at: datetime | None
    source: Source | None


class InsightsService:
    def __init__(self, context: AppContext, portfolio: PortfolioService) -> None:
        self._ctx = context
        self._portfolio = portfolio

    # ---- per portfolio ---------------------------------------------------------

    def insights(self, portfolio_id: str, *, benchmark: str | None = None) -> InsightsView:
        """Allocation, exposure, P&L and risk of one portfolio."""
        bench = (benchmark or DEFAULT_BENCHMARK).strip().upper()
        loaded = self._load(portfolio_id)
        book = loaded.book
        notes: list[str] = []
        tickers = sorted({h.ticker for h in book.priced if h.ticker})
        returns = self._returns([*tickers, bench])
        returns_as_of = _day(returns.index.max()) if not returns.empty else None
        betas = self._betas(book, returns, bench, notes)
        with self._ctx.state() as state:
            points = [(r.day, r.total_value) for r in load_pnl(state, portfolio_id=portfolio_id)]
        if book.uncovered:
            notes.append(
                f"{len(book.uncovered)} holding(s) have no ticker; their broker value counts "
                "but they have no beta, risk or signals"
            )
        return InsightsView(
            portfolio_id=portfolio_id,
            taken_at=loaded.taken_at,
            source=loaded.source,
            currency=book.base_currency,
            cash=book.cash,
            total_value=book.total_value,
            allocation=AllocationView(
                asset_class=allocation(book, "asset_class"),
                sector=allocation(book, "sector"),
                currency=allocation(book, "currency"),
                ticker=allocation(book, "ticker"),
            ),
            exposure=exposure(book, betas=betas, benchmark=bench),
            pnl=period_pnl(points),
            risk=RiskView(
                history=realized_risk([v for _, v in points]),
                holdings=self._holdings_risk(book, returns),
                concentration=concentration(book),
                returns_as_of=returns_as_of,
            ),
            uncovered=[h.symbol for h in book.uncovered],
            unpriced=[h.symbol for h in book.unpriced],
            notes=notes,
        )

    def agreement(self, portfolio_id: str) -> AgreementView:
        """For each holding, what every active strategy's latest signal says."""
        book = self._load(portfolio_id).book
        tickers = sorted({h.ticker for h in book.holdings if h.ticker})
        strategies: dict[str, Any] = {}
        skipped: list[str] = []
        with self._ctx.registry() as registry:
            for handle in registry.list_all(status="active"):
                try:
                    strategies[handle.id] = registry.load(handle.id)
                except Exception as exc:  # a broken strategy must not break the page
                    _log.warning("insights.strategy_load_failed", strategy_id=handle.id,
                                 error=str(exc))  # fmt: skip
                    skipped.append(handle.id)
        with self._ctx.lake() as lake:
            as_of = self._last_price_day(lake, tickers)
            rows = strategy_agreement(book, strategies, as_of, lake)
        return AgreementView(
            portfolio_id=portfolio_id,
            as_of=as_of,
            strategies=list(strategies),
            skipped=skipped,
            holdings=rows,
        )

    # ---- admins ----------------------------------------------------------------

    def totals(self, principal: Principal) -> InsightsTotalsView:
        """Asset-class allocation and exposure summed over every active book."""
        require(principal, Permission.PORTFOLIO_TOTALS)
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT p.id, p.owner_id FROM portfolios p WHERE p.status = 'active'"
                " AND EXISTS (SELECT 1 FROM portfolio_snapshots s WHERE s.portfolio_id = p.id)"
                " ORDER BY p.id"
            )
        cash = 0.0
        holdings: list[Holding] = []
        for r in rows:
            book = self._load(r["id"]).book
            cash += book.cash
            holdings.extend(book.holdings)
        combined = Book(cash=cash, holdings=tuple(holdings))
        return InsightsTotalsView(
            portfolios=len(rows),
            owners=len({r["owner_id"] for r in rows}),
            cash=cash,
            total_value=combined.total_value,
            asset_class=allocation(combined, "asset_class"),
            exposure=exposure(combined, betas={}, benchmark=None),
        )

    # ---- loading ---------------------------------------------------------------

    def _load(self, portfolio_id: str) -> _Loaded:
        with self._ctx.state() as state:
            base = state.sql("SELECT base_currency FROM portfolios WHERE id = ?", [portfolio_id])
            where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id)
            snap = state.sql(
                f"SELECT id, taken_at, cash, source FROM portfolio_snapshots WHERE {where}"
                " ORDER BY id DESC LIMIT 1",
                params,
            )
            broker_rows = (
                state.sql(
                    "SELECT raw_symbol, ticker, quantity, price, market_value, currency"
                    " FROM broker_positions WHERE snapshot_id = ? ORDER BY raw_symbol",
                    [snap[0]["id"]],
                )
                if snap and snap[0]["source"] == "sync"
                else []
            )
        currency = (base[0]["base_currency"] if base else None) or DEFAULT_CURRENCY
        if not snap:
            view = self._portfolio.current(portfolio_id)
            return _Loaded(Book(cash=view.cash, holdings=(), base_currency=currency), None, None)
        taken_at = datetime.fromisoformat(snap[0]["taken_at"])
        source: Source = "sync" if snap[0]["source"] == "sync" else "tick"
        if broker_rows:
            holdings = self._broker_holdings(broker_rows)
            cash = float(snap[0]["cash"])
        else:
            view = self._portfolio.current(portfolio_id)
            cash = view.cash
            holdings = [
                Holding(
                    p.ticker, p.ticker, p.quantity, p.price, p.market_value, currency=p.currency
                )
                for p in view.positions
            ]
        book = Book(cash=cash, holdings=tuple(self._enrich(holdings)), base_currency=currency)
        return _Loaded(book, taken_at, source)

    def _broker_holdings(self, rows: Sequence[Any]) -> list[Holding]:
        closes = self._latest_closes([r["ticker"] for r in rows if r["ticker"]])
        out: list[Holding] = []
        for r in rows:
            qty = float(r["quantity"])
            price = r["price"] if r["price"] is not None else closes.get(r["ticker"])
            value = r["market_value"]
            if value is None and price is not None:
                value = qty * float(price)
            out.append(
                Holding(
                    symbol=r["ticker"] or r["raw_symbol"],
                    ticker=r["ticker"],
                    quantity=qty,
                    price=None if price is None else float(price),
                    market_value=None if value is None else float(value),
                    currency=(r["currency"] or None) and str(r["currency"]).upper(),
                )
            )
        return out

    def _enrich(self, holdings: list[Holding]) -> list[Holding]:
        tickers = [h.ticker for h in holdings if h.ticker]
        if not tickers:
            return holdings
        with self._ctx.lake() as lake:
            df = lake.sql(
                "SELECT id, asset_class, sector, currency FROM instruments WHERE id = ANY(?)",
                [tickers],
            )
        info: dict[str, dict[str, Any]] = {r["id"]: r for r in df.to_dict("records")}
        out = []
        for h in holdings:
            meta = info.get(h.ticker) if h.ticker else None
            if meta is None:
                out.append(h)
                continue
            out.append(
                Holding(
                    symbol=h.symbol,
                    ticker=h.ticker,
                    quantity=h.quantity,
                    price=h.price,
                    market_value=h.market_value,
                    asset_class=_text(meta["asset_class"]),
                    sector=_text(meta["sector"]),
                    currency=h.currency or (_text(meta["currency"]) or "").upper() or None,
                )
            )
        return out

    def _latest_closes(self, tickers: list[str]) -> dict[str, float]:
        if not tickers:
            return {}
        with self._ctx.lake() as lake:
            df = lake.sql(
                "SELECT ticker, arg_max(close, date) AS close FROM prices"
                " WHERE ticker = ANY(?) GROUP BY ticker",
                [tickers],
            )
        return {str(r["ticker"]): float(r["close"]) for r in df.to_dict("records")}

    @staticmethod
    def _last_price_day(lake: Any, tickers: list[str]) -> date | None:
        if not tickers:
            return None
        df = lake.sql("SELECT max(date) AS d FROM prices WHERE ticker = ANY(?)", [tickers])
        value = df["d"].iloc[0] if not df.empty else None
        return _day(value)

    def _returns(self, tickers: list[str]) -> pd.DataFrame:
        """Daily returns (adjusted closes) of ``tickers`` over the lookback
        window ending at their latest price: one column per ticker."""
        with self._ctx.lake() as lake:
            end = self._last_price_day(lake, tickers)
            if end is None:
                return pd.DataFrame()
            df = lake.sql(
                "SELECT ticker, date, COALESCE(adj_close, close) AS px FROM prices"
                " WHERE ticker = ANY(?) AND date > ? AND date <= ? ORDER BY date",
                [tickers, end - timedelta(days=LOOKBACK_DAYS), end],
            )
        if df.empty:
            return pd.DataFrame()
        wide = df.pivot_table(index="date", columns="ticker", values="px", aggfunc="last")
        wide.index = pd.to_datetime(wide.index)
        return wide.sort_index().pct_change(fill_method=None).iloc[1:]

    @staticmethod
    def _betas(
        book: Book, returns: pd.DataFrame, bench: str, notes: list[str]
    ) -> dict[str, float | None]:
        if bench not in returns.columns:
            notes.append(f"no prices for the benchmark {bench}, so no beta")
            return {}
        out: dict[str, float | None] = {}
        for h in book.priced:
            if h.ticker is None or h.ticker not in returns.columns:
                continue
            if h.ticker == bench:
                out[h.symbol] = 1.0
                continue
            pair = returns[[h.ticker, bench]].dropna()
            out[h.symbol] = beta(pair[h.ticker].tolist(), pair[bench].tolist())
        return out

    @staticmethod
    def _holdings_risk(book: Book, returns: pd.DataFrame) -> RiskStats | None:
        total = book.total_value
        if returns.empty or total <= 0:
            return None
        weights = {
            h.ticker: (h.market_value or 0.0) / total
            for h in book.priced
            if h.ticker and h.ticker in returns.columns
        }
        if not weights:
            return None
        frame = returns[list(weights)].dropna()
        series = {t: frame[t].tolist() for t in weights}
        return returns_risk(weighted_returns(weights, series))


def _day(value: Any) -> date | None:
    """A price day from a DuckDB or pandas value; ``None`` for a missing one."""
    if value is None or pd.isna(value):
        return None
    if not isinstance(value, date):
        value = pd.Timestamp(value).to_pydatetime()
    if isinstance(value, datetime):  # pandas Timestamps are datetimes too
        return value.date()
    return value if isinstance(value, date) else None


def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value)
    return text or None
