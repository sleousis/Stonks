"""Calendar and news tools (roadmap 20.7): earnings, ex-dividend dates and
economic releases for your holdings, a watchlist or some tickers, the news
and sentiment of those tickers, and the earnings check an order ticket
shows. Reads only. The refresh is an operator job the scheduler runs."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from pydantic import Field

from stonks.mcp.tools.common import (
    READ,
    IsoDate,
    PortfolioId,
    ToolContext,
    drop_none,
    iso,
    items,
)

Scope = Annotated[
    Literal["holdings", "watchlists", "tickers", "all"],
    Field(description="holdings (default), watchlists, tickers or all"),
]
ScopeTickers = Annotated[
    list[str] | None,
    Field(max_length=500, description="instrument ids (scope tickers)"),
]
WatchlistId = Annotated[
    str | None, Field(max_length=64, description="one of your watchlists (scope watchlists)")
]


def _query(
    scope: str,
    tickers: list[str] | None,
    watchlist_id: str | None,
    portfolio_id: str | None,
) -> dict[str, Any]:
    return drop_none(
        {
            "scope": scope,
            "tickers": ",".join(tickers) if tickers else None,
            "watchlist_id": watchlist_id,
            "portfolio_id": portfolio_id,
        }
    )


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def get_calendar(
        scope: Scope = "holdings",
        tickers: ScopeTickers = None,
        watchlist_id: WatchlistId = None,
        portfolio_id: PortfolioId = None,
        start: IsoDate | None = None,
        end: IsoDate | None = None,
        countries: Annotated[
            list[str] | None,
            Field(max_length=100, description="economic events of these ISO alpha-2 codes"),
        ] = None,
    ) -> dict[str, Any]:
        """Upcoming earnings (with the time of day and the EPS estimate),
        ex-dividend dates and economic releases from ``start`` (default
        today) to ``end`` (default two weeks on, at most 120 days).
        Economic releases are market wide."""
        query = _query(scope, tickers, watchlist_id, portfolio_id) | drop_none(
            {
                "start": iso(start),
                "end": iso(end),
                "countries": ",".join(countries) if countries else None,
            }
        )
        return await t.get("/api/calendars", params=query)

    @server.tool(annotations=READ)
    async def get_news(
        scope: Scope = "holdings",
        tickers: ScopeTickers = None,
        watchlist_id: WatchlistId = None,
        portfolio_id: PortfolioId = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """The newest articles on the scope's tickers and their daily
        sentiment over the last 30 days. Scope all is refused."""
        query = _query(scope, tickers, watchlist_id, portfolio_id) | {"limit": limit}
        return await t.get("/api/calendars/news", params=query)

    @server.tool(annotations=READ)
    async def get_earnings_warnings(
        tickers: Annotated[list[str], Field(min_length=1, max_length=500)],
    ) -> dict[str, Any]:
        """Which of ``tickers`` report earnings before the next open of
        their market. An order placed now fills after the report."""
        return await t.get(
            "/api/calendars/earnings-warnings", params={"tickers": ",".join(tickers)}
        )

    @server.tool(annotations=READ)
    async def list_event_alert_kinds() -> dict[str, Any]:
        """The upcoming-event alert kinds (earnings, ex-dividend) and how
        many days ahead each looks by default."""
        return items(await t.get("/api/calendars/alert-kinds"))
