"""IntradayPnlService: intraday P&L and risk snapshots (roadmap 21.3.3) for
the API and MCP. The math and the rows live in
:mod:`stonks.production.intraday_pnl`. This module reads one portfolio,
resolved by the caller (the API's ``PortfolioIdDep`` answers 404 for another
user's book)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page
from stonks.production.intraday_pnl import (
    PORTFOLIO_BOOK,
    IntradaySnapshot,
    intraday_snapshots_enabled,
    latest_intraday_day,
    list_intraday_snapshots,
)


class IntradaySnapshotView(BaseModel):
    """One book at one moment of a session. ``strategy_id`` is null for the
    whole portfolio. Money is in the book's currency."""

    portfolio_id: str
    strategy_id: str | None
    #: The trading day (UTC date of the bar close).
    day: date
    #: The bar close the row was computed at (UTC).
    at: datetime
    #: Portfolio: cash plus positions at the prior close. Sleeve: gross exposure.
    start_value: float
    #: Portfolio: start value plus P&L. Sleeve: gross exposure now.
    value: float
    #: From fills that reduced a position, at average cost.
    realised: float
    #: Open positions at the latest marks against their cost.
    unrealised: float
    fees: float
    #: Realised plus unrealised less fees.
    pnl: float
    day_return: float | None
    #: The day's best P&L so far.
    high_water_pnl: float
    #: The drop from that high as a fraction of the value there (zero or less).
    drawdown: float
    gross_exposure: float
    net_exposure: float
    exposures: dict[str, float]
    fills: int
    #: Held tickers with no mark yet, valued at cost.
    unmarked: int
    #: Held tickers whose mark is older than the limit.
    stale_marks: int
    max_mark_age_seconds: float | None

    @classmethod
    def of(cls, snap: IntradaySnapshot) -> IntradaySnapshotView:
        return cls(**snap.as_dict())


class IntradayPnlService:
    def __init__(self, context: AppContext) -> None:
        self._context = context

    def list(
        self,
        portfolio_id: str,
        *,
        day: date | None = None,
        strategy_id: str | None = None,
        all_books: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IntradaySnapshotView]:
        """``portfolio_id``'s rows on ``day`` (default its latest day with
        rows), newest first: the whole portfolio (default), one strategy's
        sleeve, or every book with ``all_books``."""
        empty = Page[IntradaySnapshotView](items=[], total=0, limit=limit, offset=offset)
        book = None if all_books else (strategy_id or PORTFOLIO_BOOK)
        with self._context.state() as state:
            if not intraday_snapshots_enabled(state):
                return empty
            on = day or latest_intraday_day(state, portfolio_id)
            if on is None:
                return empty
            snaps, total = list_intraday_snapshots(
                state, portfolio_id, on, strategy_id=book, limit=limit, offset=offset
            )
        return Page[IntradaySnapshotView](
            items=[IntradaySnapshotView.of(s) for s in snaps],
            total=total,
            limit=limit,
            offset=offset,
        )
