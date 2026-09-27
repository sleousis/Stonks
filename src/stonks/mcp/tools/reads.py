"""Read tools: GETs only (``READ`` annotation).

A parameterless route is a :class:`RouteRead` row in :data:`ROUTE_READS`;
a route with query or path parameters is one small function in
:func:`register`.
"""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from pydantic import Field

from stonks.mcp.tools.common import (
    READ,
    AssetClass,
    IsoDate,
    JobStatus,
    Limit,
    Offset,
    PortfolioId,
    RouteRead,
    Since,
    StrategyStatus,
    Ticker,
    ToolContext,
    iso,
    items,
    register_route_reads,
    seg,
)

ROUTE_READS: tuple[RouteRead, ...] = (
    RouteRead(
        "get_api_health", "/api/health", "Check that the Stonks API is up and report its version."
    ),
    RouteRead(
        "list_survival_tests",
        "/api/lab/survival-tests",
        "Every survival test a lab run can name (the ids survival_tests takes), with what "
        "it checks and its options schema (the keys test_options takes).",
    ),
    RouteRead(
        "list_survival_presets",
        "/api/lab/survival-presets",
        "The named survival suites (quick, standard, promotion) and the tests in each.",
    ),
    RouteRead(
        "whoami",
        "/api/auth/me",
        "Who this MCP server acts as: the token's user, role and scopes. Every tool does "
        "only what that user may do.",
    ),
    RouteRead(
        "get_portfolio_totals",
        "/api/portfolio/totals",
        "Admins only: cash and value summed across every active portfolio (no holdings).",
    ),
    RouteRead(
        "get_risk_policy",
        "/api/risk/policy",
        "The [production.risk] limits applied between a strategy's orders and the broker "
        "(max weights, order caps, ...).",
    ),
    RouteRead(
        "get_broker",
        "/api/brokers",
        "The broker production ticks trade through: kind (simulated/alpaca), paper, "
        "allow_live and whether credentials are configured (keys are never shown). Read-only.",
    ),
    RouteRead(
        "list_sources",
        "/api/sources",
        "Market-data sources an ingest can name, which is the default, and whether each "
        "is configured.",
    ),
    RouteRead(
        "list_cost_models",
        "/api/lab/cost-models",
        "Transaction-cost presets run_backtest accepts as cost_model, with their settings.",
    ),
)


def register(t: ToolContext) -> None:
    register_route_reads(t, ROUTE_READS)
    server = t.server

    @server.tool(annotations=READ)
    async def get_portfolio(portfolio_id: PortfolioId = None) -> dict[str, Any]:
        """Current portfolio: cash, positions valued at the latest stored closes,
        weights and total value, from the latest snapshot."""
        return await t.get("/api/portfolio", {"portfolio_id": portfolio_id})

    @server.tool(annotations=READ)
    async def list_portfolio_snapshots(
        portfolio_id: PortfolioId = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """Portfolio history: one snapshot per production tick, newest first."""
        return await t.get(
            "/api/portfolio/snapshots",
            {"portfolio_id": portfolio_id, "limit": limit, "offset": offset},
        )

    @server.tool(annotations=READ)
    async def list_strategies(
        status: StrategyStatus | None = None,
        q: Annotated[
            str | None, Field(description="case-insensitive substring of the id or class path")
        ] = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Registered strategies (id, class, params, status), optionally by status
        or a search term."""
        return await t.get(
            "/api/strategies", {"status": status, "q": q, "limit": limit, "offset": offset}
        )

    @server.tool(annotations=READ)
    async def get_strategy(strategy_id: str) -> dict[str, Any]:
        """One registered strategy with its survival-test reports (pass/fail and metrics)."""
        return await t.get(f"/api/strategies/{seg(strategy_id)}")

    @server.tool(annotations=READ)
    async def get_strategy_history(strategy_id: str) -> dict[str, Any]:
        """A strategy's audited status changes (actor, reason, override, go-live
        result), oldest first."""
        return items(await t.get(f"/api/strategies/{seg(strategy_id)}/history"))

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
        portfolio_id: PortfolioId = None,
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
                "portfolio_id": portfolio_id,
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
        portfolio_id: PortfolioId = None,
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
                "portfolio_id": portfolio_id,
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
        """Production tick runs, newest first. Summaries show the global
        outcome and the parts about your own portfolios only."""
        return await t.get("/api/ticks", {"status": status, "limit": limit, "offset": offset})

    @server.tool(annotations=READ)
    async def get_tick(tick_id: str, portfolio_id: PortfolioId = None) -> dict[str, Any]:
        """One production tick run with the orders it placed in your
        portfolios (or in ``portfolio_id`` only)."""
        return await t.get(f"/api/ticks/{seg(tick_id)}", {"portfolio_id": portfolio_id})

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
    async def list_statement_flags(
        ticker: Ticker | None = None,
        severity: Annotated[
            Literal["error", "warning"] | None, Field(description="only this severity")
        ] = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Statement periods the accounting audit flagged (BL-36): balance
        identity, net income and cash mismatches, quarters vs annual, filings
        dated before period end, negative shares. Error flags make lab
        preflight warn."""
        return await t.get(
            "/api/statements/flags",
            {"ticker": ticker, "severity": severity, "limit": limit, "offset": offset},
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

    @server.tool(annotations=READ)
    async def get_pnl(since: Since = None, portfolio_id: PortfolioId = None) -> dict[str, Any]:
        """Daily P&L of one of your portfolios (last snapshot per UTC day). Returns
        and drawdown are measured from inception even when since trims the rows."""
        return await t.get("/api/pnl", {"since": iso(since), "portfolio_id": portfolio_id})

    @server.tool(annotations=READ)
    async def list_shadow_decisions(
        strategy_id: str | None = None,
        ticker: str | None = None,
        as_of: IsoDate | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Virtual orders shadow strategies placed (never sent to a broker), newest first."""
        return await t.get(
            "/api/shadow/decisions",
            {
                "strategy_id": strategy_id,
                "ticker": ticker,
                "as_of": iso(as_of),
                "limit": limit,
                "offset": offset,
            },
        )

    @server.tool(annotations=READ)
    async def list_shadow_pnl(
        since: Since = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """Latest value, return and worst drawdown of every shadow strategy's
        virtual portfolio."""
        return await t.get(
            "/api/shadow/pnl", {"since": iso(since), "limit": limit, "offset": offset}
        )

    @server.tool(annotations=READ)
    async def get_shadow_pnl(strategy_id: str, since: Since = None) -> dict[str, Any]:
        """Daily P&L of one shadow strategy's virtual portfolio."""
        return await t.get(f"/api/shadow/strategies/{seg(strategy_id)}/pnl", {"since": iso(since)})

    @server.tool(annotations=READ)
    async def get_golive_report(
        strategy_id: str,
        since: Annotated[
            IsoDate | None, Field(description="first day of the paper period; default all")
        ] = None,
    ) -> dict[str, Any]:
        """The go-live gate for one strategy: every check of its paper period
        (days, trades, drawdown, Sharpe against the backtest, MinTRL, ...)
        with its value, limit and pass or fail, and the verdict. Read it
        before promote_strategy: an override skips exactly these checks."""
        return await t.get(f"/api/strategies/{seg(strategy_id)}/golive", {"since": iso(since)})

    @server.tool(annotations=READ)
    async def list_ledger_runs(
        strategy_class: Annotated[
            str | None, Field(max_length=200, description="only runs of this module:Class")
        ] = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """The trial ledger: every recorded lab run, newest first, with its
        hypothesis, tuner, budget, trials run and failed, best score and
        verdict. Trials of a class add up across runs, which is what the
        deflated Sharpe and other multiple-testing checks count."""
        return await t.get(
            "/api/lab/ledger",
            {"strategy_class": strategy_class, "limit": limit, "offset": offset},
        )

    @server.tool(annotations=READ)
    async def get_ledger_run(run_id: str) -> dict[str, Any]:
        """One recorded lab run with every trial (parameters, score, status)
        and the trial count of its strategy class across all runs."""
        return await t.get(f"/api/lab/ledger/{seg(run_id)}")

    @server.tool(annotations=READ)
    async def get_schedule(
        limit: Annotated[int, Field(ge=1, le=200, description="recent runs to show")] = 20,
    ) -> dict[str, Any]:
        """The scheduler: each job with its next run and last outcome, the
        most recent runs (did last night's trading run go?), and the market
        session today and next (open, close, and whether it is open now)."""
        return await t.get("/api/schedule", {"limit": limit})

    @server.tool(annotations=READ)
    async def list_alerts(
        level: Literal["info", "warning", "error"] | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """System alerts for you, newest first (admins also see operational
        alerts with no single recipient): failed runs, halts, stale data."""
        return await t.get("/api/alerts", {"level": level, "limit": limit, "offset": offset})

    @server.tool(annotations=READ)
    async def get_health_report(
        tickers: Annotated[
            list[str] | None,
            Field(description="bar-freshness tickers; default [production].universe"),
        ] = None,
    ) -> dict[str, Any]:
        """Operational health (every `stonks health` check): bar freshness, stuck
        ticks and ingest runs, recent ingest failures; see `healthy`."""
        return await t.get("/api/health/report", {"tickers": tickers})
