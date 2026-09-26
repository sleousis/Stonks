"""Read tools: GETs only (``READ`` annotation).

A parameterless route is a :class:`RouteRead` row in :data:`ROUTE_READS`;
a route with query or path parameters is one small function in
:func:`register`.
"""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from pydantic import Field

from stonks.mcp.tools.common import (
    READ,
    AssetClass,
    IsoDate,
    JobStatus,
    Limit,
    Offset,
    RouteRead,
    StrategyStatus,
    Ticker,
    ToolContext,
    iso,
    register_route_reads,
    seg,
)

ROUTE_READS: tuple[RouteRead, ...] = (
    RouteRead("health", "/api/health", "Check that the Stonks API is up and report its version."),
    RouteRead(
        "get_portfolio",
        "/api/portfolio",
        "Current portfolio: cash, positions valued at the latest stored closes, "
        "weights and total value, from the latest snapshot.",
    ),
)


def register(t: ToolContext) -> None:
    register_route_reads(t, ROUTE_READS)
    server = t.server

    @server.tool(annotations=READ)
    async def list_portfolio_snapshots(limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Portfolio history: one snapshot per production tick, newest first."""
        return await t.get("/api/portfolio/snapshots", {"limit": limit, "offset": offset})

    @server.tool(annotations=READ)
    async def list_strategies(
        status: StrategyStatus | None = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """Registered strategies (id, class, params, status), optionally by status."""
        return await t.get("/api/strategies", {"status": status, "limit": limit, "offset": offset})

    @server.tool(annotations=READ)
    async def get_strategy(strategy_id: str) -> dict[str, Any]:
        """One registered strategy with its survival-test reports (pass/fail and metrics)."""
        return await t.get(f"/api/strategies/{seg(strategy_id)}")

    @server.tool(annotations=READ)
    async def search_instruments(
        q: Annotated[str | None, Field(description="substring of id or name")] = None,
        asset_class: AssetClass | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Search instruments in the lake by id/name and asset class."""
        return await t.get(
            "/api/market/instruments",
            {"q": q, "asset_class": asset_class, "limit": limit, "offset": offset},
        )

    @server.tool(annotations=READ)
    async def get_bars(
        ticker: Ticker,
        interval: Annotated[str, Field(description="bar interval, e.g. 1d, 1h, 5m")] = "1d",
        start: IsoDate | None = None,
        end: IsoDate | None = None,
        limit: Annotated[int | None, Field(ge=1, description="max bars")] = None,
    ) -> dict[str, Any]:
        """OHLCV bars for one instrument at one interval."""
        return await t.get(
            "/api/market/bars",
            {
                "ticker": ticker,
                "interval": interval,
                "start": iso(start),
                "end": iso(end),
                "limit": limit,
            },
        )

    @server.tool(annotations=READ)
    async def get_coverage(
        ticker: str | None = None,
        interval: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Data coverage: first/last bar and bar count per instrument and interval."""
        return await t.get(
            "/api/market/coverage",
            {"ticker": ticker, "interval": interval, "limit": limit, "offset": offset},
        )

    @server.tool(annotations=READ)
    async def list_orders(
        tick_id: str | None = None,
        strategy_id: str | None = None,
        ticker: str | None = None,
        status: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Orders placed by production ticks, newest first, with optional filters."""
        return await t.get(
            "/api/orders",
            {
                "tick_id": tick_id,
                "strategy_id": strategy_id,
                "ticker": ticker,
                "status": status,
                "limit": limit,
                "offset": offset,
            },
        )

    @server.tool(annotations=READ)
    async def list_fills(
        tick_id: str | None = None,
        ticker: str | None = None,
        order_client_id: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Fills (executions) of orders, with optional filters."""
        return await t.get(
            "/api/orders/fills",
            {
                "tick_id": tick_id,
                "ticker": ticker,
                "order_client_id": order_client_id,
                "limit": limit,
                "offset": offset,
            },
        )

    @server.tool(annotations=READ)
    async def list_ticks(
        status: str | None = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """Production tick runs, newest first, with their summaries."""
        return await t.get("/api/ticks", {"status": status, "limit": limit, "offset": offset})

    @server.tool(annotations=READ)
    async def get_tick(tick_id: str) -> dict[str, Any]:
        """One production tick run with the orders it placed."""
        return await t.get(f"/api/ticks/{seg(tick_id)}")

    @server.tool(annotations=READ)
    async def list_ingest_runs(
        kind: str | None = None,
        status: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Market-data ingest runs (source, kind, tickers ok/failed, status)."""
        return await t.get(
            "/api/ingest/runs", {"kind": kind, "status": status, "limit": limit, "offset": offset}
        )

    @server.tool(annotations=READ)
    async def get_catalog() -> dict[str, Any]:
        """Strategy classes that can be backtested or tuned, with their parameter
        specs, plus the valid bar intervals and asset classes."""
        return {
            "strategies": await t.get("/api/catalog/strategies"),
            "intervals": await t.get("/api/catalog/intervals"),
            "asset_classes": await t.get("/api/catalog/asset-classes"),
        }

    @server.tool(annotations=READ)
    async def list_jobs(
        status: JobStatus | None = None,
        kind: Annotated[
            str | None,
            Field(description="backtest, lab_run, ingest, tick, studio_backtest, studio_lab_run"),
        ] = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Background jobs (backtests, lab runs, ingests, ticks), newest first."""
        return await t.get(
            "/api/jobs", {"status": status, "kind": kind, "limit": limit, "offset": offset}
        )

    @server.tool(annotations=READ)
    async def get_job(job_id: str) -> dict[str, Any]:
        """One background job: status, progress, and its result once finished."""
        return await t.get(f"/api/jobs/{seg(job_id)}")
