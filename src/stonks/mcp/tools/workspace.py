"""Trader workspace tools (roadmap 13.4 to 13.8): charts, the strategy
leaderboard and tear sheets, your watchlists and your risk limits.

Reads, plus creating and editing your own watchlists (harmless, no
confirm). Deleting a watchlist, changing your risk limits and the
first-run guide stay in the console."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from pydantic import Field

from stonks.mcp.tools.common import (
    EDIT,
    READ,
    WRITE,
    PortfolioId,
    Ticker,
    ToolContext,
    drop_none,
    items,
    seg,
)

Day = Annotated[str | None, Field(description="YYYY-MM-DD")]
WatchlistName = Annotated[str, Field(min_length=1, max_length=80, description="the list's name")]
WatchlistTickers = Annotated[
    list[str], Field(max_length=500, description="instrument ids, kept once and in order")
]
SortKey = Annotated[
    Literal["sharpe", "return", "drawdown", "trades"],
    Field(description="rank by paper Sharpe, total return, drawdown or trades"),
]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def get_chart(
        ticker: Ticker,
        interval: str = "1d",
        start: Day = None,
        end: Day = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 750,
        portfolio_id: PortfolioId = None,
        strategy_id: Annotated[
            str | None, Field(description="only this strategy's signals")
        ] = None,
    ) -> dict[str, Any]:
        """A price chart's data for one ticker: OHLCV bars (the latest
        ``limit`` in the window), your fills of it (side, quantity, price)
        in one of your portfolios, and every strategy's signal events for it
        (entry, exit, increase, decrease) with the plain reason."""
        query = drop_none(
            {
                "interval": interval,
                "start": start,
                "end": end,
                "limit": limit,
                "portfolio_id": portfolio_id,
                "strategy_id": strategy_id,
            }
        )
        return await t.get(f"/api/charts/{seg(ticker)}", params=query)

    @server.tool(annotations=READ)
    async def get_leaderboard(
        sort: SortKey = "sharpe", include_retired: bool = False
    ) -> dict[str, Any]:
        """Every strategy ranked by its risk-adjusted paper result (the
        model book the tick keeps for it): total return, CAGR, Sharpe,
        Sortino, worst drawdown, trades in the model book and in real books,
        survival tests passed and the go-live verdict."""
        return await t.get(
            "/api/strategies/leaderboard",
            params={"sort": sort, "include_retired": include_retired},
        )

    @server.tool(annotations=READ)
    async def get_tear_sheet(strategy_id: str) -> dict[str, Any]:
        """One strategy on one page: paper figures and value curve, monthly
        returns, recent model-book trades, survival verdicts, the go-live
        report and the status history."""
        return await t.get(f"/api/strategies/{seg(strategy_id)}/tearsheet")

    @server.tool(annotations=READ)
    async def list_watchlists() -> dict[str, Any]:
        """Your watchlists (named ticker lists), oldest first."""
        return items(await t.get("/api/watchlists", params={"limit": 500}))

    @server.tool(annotations=READ)
    async def get_watchlist(watchlist_id: str) -> dict[str, Any]:
        """One of your watchlists with its tickers."""
        return await t.get(f"/api/watchlists/{seg(watchlist_id)}")

    @server.tool(annotations=WRITE)
    async def create_watchlist(
        name: WatchlistName, tickers: WatchlistTickers | None = None
    ) -> dict[str, Any]:
        """Start a watchlist of yours. Names are unique per person. Needs a
        trading token."""
        return await t.post("/api/watchlists", {"name": name, "tickers": tickers or []})

    @server.tool(annotations=EDIT)
    async def update_watchlist(
        watchlist_id: str,
        name: Annotated[str | None, Field(min_length=1, max_length=80)] = None,
        tickers: WatchlistTickers | None = None,
    ) -> dict[str, Any]:
        """Rename one of your watchlists, replace its tickers, or both."""
        body = drop_none({"name": name, "tickers": tickers})
        return await t.patch(f"/api/watchlists/{seg(watchlist_id)}", body)

    @server.tool(annotations=READ)
    async def get_my_risk_limits() -> dict[str, Any]:
        """Your own risk limits next to the system policy, what your
        portfolios follow (the system tightened by yours) and any of yours
        that are looser than the system and so change nothing."""
        return await t.get("/api/risk/limits")
