"""MCP tools and resources over the Stonks REST API (official ``mcp`` SDK).

Every tool is a small async function that calls :class:`ApiClient` and
returns plain JSON-able data; adding a tool for a new route is one more
function in the matching ``_register_*`` block. ``mcp`` types stay in this
module.

Safety model
------------
- Read tools only issue GETs.
- Job tools (backtest, lab run, ingest) queue background work on the API;
  they write research data only, never orders.
- Guarded tools (promote/retire/shadow, run_tick) need ``confirm=true``;
  without it they return a preview and send nothing mutating.
  ``run_tick`` defaults to ``dry_run=true``, and a real tick is refused
  unless the API positively reports the broker is not trading real money.
- No tool changes broker settings, configuration, or enables live trading.
"""

# No ``from __future__ import annotations``: tool signatures use closure
# values inside ``Annotated`` metadata, which must be evaluated at def time.

import json
import time
from collections.abc import Awaitable
from datetime import date
from typing import Annotated, Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.client import ApiClient, ApiError, segment
from stonks.mcp.guards import CONFIRM_HINT, live_trading_state, status_change_preview

# --- annotations --------------------------------------------------------------

READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
JOB = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
# Ingest reaches out to external market-data vendors via the API.
JOB_OPEN_WORLD = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
STATUS_CHANGE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
TICK = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

#: Broker the production tick trades through (``BrokerInfo``). A
#: non-dry-run tick needs it to report a simulated or paper broker.
BROKER_STATUS_PATH = "/api/brokers"

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

INSTRUCTIONS = """\
Stonks research + trading system. Tools talk to the local REST API started
with `stonks serve`. Read tools are safe. Job tools queue backtests, lab runs
and ingests; follow up with wait_for_job. Status changes and production ticks
need confirm=true; call them first without it to get a preview and show it to
the user before confirming. run_tick is a dry run unless dry_run=false, and a
real tick is refused unless the API reports a paper/simulated broker. No tool
can change broker settings or enable live trading."""

# --- shared parameter types ------------------------------------------------------

Limit = Annotated[int, Field(ge=1, le=500, description="page size")]
Offset = Annotated[int, Field(ge=0, description="rows to skip")]
Ticker = Annotated[str, Field(description="instrument id, e.g. AAPL.US or BTC-USD.CC")]
Tickers = Annotated[list[str], Field(min_length=1, description="instrument ids")]
IsoDate = Annotated[date, Field(description="YYYY-MM-DD")]
Confirm = Annotated[
    bool,
    Field(description="must be true to apply; false (default) returns a preview only"),
]
StrategyStatus = Literal["active", "shadow", "retired"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
AssetClass = Literal["equity", "crypto", "commodity", "bond"]


def _seg(value: str) -> str:
    """Path-safe id (see :func:`segment`), as a tool error when invalid."""
    try:
        return segment(value)
    except ApiError as exc:
        raise ToolError(str(exc)) from None


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _drop_none(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


def _strategy_ref(
    strategy_id: str | None, class_path: str | None, params: dict[str, Any] | None
) -> dict[str, Any]:
    if (strategy_id is None) == (class_path is None):
        raise ToolError("set exactly one of strategy_id or class_path")
    return _drop_none({"strategy_id": strategy_id, "class_path": class_path, "params": params})


def build_server(api: ApiClient, *, max_wait_seconds: float = 600.0) -> MCPServer:
    server = MCPServer("stonks", instructions=INSTRUCTIONS)

    async def call(awaitable: Awaitable[Any]) -> Any:
        """Run one API call, turning API failures into tool errors whose
        text never contains the token."""
        try:
            return await awaitable
        except ApiError as exc:
            raise ToolError(api.redact(str(exc))) from None

    _register_read_tools(server, api, call)
    _register_job_tools(server, api, call, max_wait_seconds)
    _register_guarded_tools(server, api, call)
    _register_resources(server, api, call)
    return server


# --- read tools -------------------------------------------------------------------


def _register_read_tools(server: MCPServer, api: ApiClient, call) -> None:
    @server.tool(annotations=READ)
    async def health() -> dict[str, Any]:
        """Check that the Stonks API is up and report its version."""
        return await call(api.get("/api/health"))

    @server.tool(annotations=READ)
    async def get_portfolio() -> dict[str, Any]:
        """Current portfolio: cash, positions valued at the latest stored closes,
        weights and total value, from the latest snapshot."""
        return await call(api.get("/api/portfolio"))

    @server.tool(annotations=READ)
    async def list_portfolio_snapshots(limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Portfolio history: one snapshot per production tick, newest first."""
        return await call(api.get("/api/portfolio/snapshots", {"limit": limit, "offset": offset}))

    @server.tool(annotations=READ)
    async def list_strategies(
        status: StrategyStatus | None = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """Registered strategies (id, class, params, status), optionally by status."""
        return await call(
            api.get("/api/strategies", {"status": status, "limit": limit, "offset": offset})
        )

    @server.tool(annotations=READ)
    async def get_strategy(strategy_id: str) -> dict[str, Any]:
        """One registered strategy with its survival-test reports (pass/fail and metrics)."""
        return await call(api.get(f"/api/strategies/{_seg(strategy_id)}"))

    @server.tool(annotations=READ)
    async def search_instruments(
        q: Annotated[str | None, Field(description="substring of id or name")] = None,
        asset_class: AssetClass | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Search instruments in the lake by id/name and asset class."""
        return await call(
            api.get(
                "/api/market/instruments",
                {"q": q, "asset_class": asset_class, "limit": limit, "offset": offset},
            )
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
        return await call(
            api.get(
                "/api/market/bars",
                {
                    "ticker": ticker,
                    "interval": interval,
                    "start": _iso(start),
                    "end": _iso(end),
                    "limit": limit,
                },
            )
        )

    @server.tool(annotations=READ)
    async def get_coverage(
        ticker: str | None = None,
        interval: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Data coverage: first/last bar and bar count per instrument and interval."""
        return await call(
            api.get(
                "/api/market/coverage",
                {"ticker": ticker, "interval": interval, "limit": limit, "offset": offset},
            )
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
        return await call(
            api.get(
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
        return await call(
            api.get(
                "/api/orders/fills",
                {
                    "tick_id": tick_id,
                    "ticker": ticker,
                    "order_client_id": order_client_id,
                    "limit": limit,
                    "offset": offset,
                },
            )
        )

    @server.tool(annotations=READ)
    async def list_ticks(
        status: str | None = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """Production tick runs, newest first, with their summaries."""
        return await call(
            api.get("/api/ticks", {"status": status, "limit": limit, "offset": offset})
        )

    @server.tool(annotations=READ)
    async def get_tick(tick_id: str) -> dict[str, Any]:
        """One production tick run with the orders it placed."""
        return await call(api.get(f"/api/ticks/{_seg(tick_id)}"))

    @server.tool(annotations=READ)
    async def list_ingest_runs(
        kind: str | None = None,
        status: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Market-data ingest runs (source, kind, tickers ok/failed, status)."""
        return await call(
            api.get(
                "/api/ingest/runs",
                {"kind": kind, "status": status, "limit": limit, "offset": offset},
            )
        )

    @server.tool(annotations=READ)
    async def get_catalog() -> dict[str, Any]:
        """Strategy classes that can be backtested or tuned, with their parameter
        specs, plus the valid bar intervals and asset classes."""
        return {
            "strategies": await call(api.get("/api/catalog/strategies")),
            "intervals": await call(api.get("/api/catalog/intervals")),
            "asset_classes": await call(api.get("/api/catalog/asset-classes")),
        }

    @server.tool(annotations=READ)
    async def list_jobs(
        status: JobStatus | None = None,
        kind: Annotated[str | None, Field(description="backtest, lab_run, ingest or tick")] = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Background jobs (backtests, lab runs, ingests, ticks), newest first."""
        return await call(
            api.get("/api/jobs", {"status": status, "kind": kind, "limit": limit, "offset": offset})
        )

    @server.tool(annotations=READ)
    async def get_job(job_id: str) -> dict[str, Any]:
        """One background job: status, progress, and its result once finished."""
        return await call(api.get(f"/api/jobs/{_seg(job_id)}"))


# --- job tools --------------------------------------------------------------------


def _register_job_tools(server: MCPServer, api: ApiClient, call, max_wait: float) -> None:
    @server.tool(annotations=JOB)
    async def run_backtest(
        universe: Tickers,
        start: IsoDate,
        end: IsoDate,
        strategy_id: Annotated[
            str | None, Field(description="registered strategy id (or use class_path)")
        ] = None,
        class_path: Annotated[
            str | None, Field(description="catalog class path, e.g. pkg.mod:Class")
        ] = None,
        params: Annotated[
            dict[str, Any] | None, Field(description="strategy params (with class_path)")
        ] = None,
        interval: str = "1d",
        initial_cash: Annotated[float, Field(gt=0)] = 10_000.0,
        threshold: float = 0.0,
        rebalance_every_bars: Annotated[int, Field(ge=1)] = 1,
        slippage_bps: Annotated[float, Field(ge=0)] = 0.0,
        fee_per_trade: Annotated[float, Field(ge=0)] = 0.0,
    ) -> dict[str, Any]:
        """Queue a backtest of one strategy over a universe and date window.
        Returns the job; use wait_for_job to get the metrics and equity curve.
        Simulated only: never places real orders."""
        body = {
            "strategy": _strategy_ref(strategy_id, class_path, params),
            "universe": universe,
            "start": _iso(start),
            "end": _iso(end),
            "interval": interval,
            "initial_cash": initial_cash,
            "threshold": threshold,
            "rebalance_every_bars": rebalance_every_bars,
            "slippage_bps": slippage_bps,
            "fee_per_trade": fee_per_trade,
        }
        return await call(api.post("/api/lab/backtests", body))

    @server.tool(annotations=JOB)
    async def run_lab(
        class_path: Annotated[str, Field(description="catalog class path to tune")],
        universe: Tickers,
        start: IsoDate,
        end: IsoDate,
        survival_tests: Annotated[
            list[
                Literal[
                    "oos", "period_stability", "perturbation", "drift", "runs_test", "permutation"
                ]
            ]
            | None,
            Field(description="survival suite; server default when omitted"),
        ] = None,
        tuner: Literal["grid", "random"] = "random",
        objective: Literal["sharpe", "cagr", "final_return"] = "sharpe",
        budget: Annotated[int, Field(ge=1, le=1000, description="tuner trials")] = 20,
        train_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.7,
        interval: str = "1d",
        seed: int = 0,
        register_strategy: Annotated[
            bool, Field(description="register the tuned strategy (lands in shadow status)")
        ] = False,
    ) -> dict[str, Any]:
        """Queue a lab run: tune a strategy class, fit, run the survival suite and
        give a pass/fail verdict. Returns the job; use wait_for_job for the result."""
        body = _drop_none(
            {
                "strategy": {"class_path": class_path},
                "universe": universe,
                "start": _iso(start),
                "end": _iso(end),
                "survival_tests": survival_tests,
                "tuner": tuner,
                "objective": objective,
                "budget": budget,
                "train_ratio": train_ratio,
                "interval": interval,
                "seed": seed,
                "register_strategy": register_strategy,
            }
        )
        return await call(api.post("/api/lab/runs", body))

    @server.tool(annotations=JOB_OPEN_WORLD)
    async def run_ingest(
        kind: Literal["prices", "intraday", "fundamentals", "metadata"],
        tickers: Annotated[
            list[str] | None, Field(description="instrument ids; or use exchange")
        ] = None,
        exchange: Annotated[str | None, Field(description="ingest a whole exchange")] = None,
        since: IsoDate | None = None,
        until: IsoDate | None = None,
        interval: Annotated[str | None, Field(description="for intraday, e.g. 5m")] = None,
    ) -> dict[str, Any]:
        """Queue a market-data ingest into the lake from the configured vendor.
        Returns the job; use wait_for_job for the outcome."""
        body = _drop_none(
            {
                "kind": kind,
                "tickers": tickers,
                "exchange": exchange,
                "since": _iso(since),
                "until": _iso(until),
                "interval": interval,
            }
        )
        return await call(api.post("/api/ingest/runs", body))

    @server.tool(annotations=READ)
    async def wait_for_job(
        job_id: str,
        timeout_seconds: Annotated[
            float, Field(gt=0, description=f"give up after this long (max {max_wait:g})")
        ] = 120.0,
        poll_seconds: Annotated[float, Field(ge=0.05, le=30)] = 1.0,
    ) -> dict[str, Any]:
        """Poll a job until it finishes or the timeout passes. Returns
        {"timed_out": bool, "job": {...}}; the job carries its result or error."""
        deadline = time.monotonic() + min(timeout_seconds, max_wait)
        while True:
            job = await call(api.get(f"/api/jobs/{_seg(job_id)}"))
            if job.get("status") in TERMINAL_JOB_STATUSES:
                return {"timed_out": False, "job": job}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"timed_out": True, "job": job}
            await anyio.sleep(min(poll_seconds, remaining))


# --- guarded write tools ------------------------------------------------------------


def _register_guarded_tools(server: MCPServer, api: ApiClient, call) -> None:
    async def change_status(strategy_id: str, action: str, target: str, confirm: bool):
        current = await call(api.get(f"/api/strategies/{_seg(strategy_id)}"))
        if not confirm:
            return status_change_preview(current, target)
        updated = await call(api.post(f"/api/strategies/{_seg(strategy_id)}/{action}"))
        return {
            "preview": False,
            "applied": True,
            "previous_status": current.get("status"),
            "strategy": updated,
        }

    @server.tool(annotations=STATUS_CHANGE)
    async def promote_strategy(strategy_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Promote a strategy to active so production ticks rank and trade it.
        Without confirm=true returns a preview (current status, survival
        results, warnings) and changes nothing."""
        return await change_status(strategy_id, "promote", "active", confirm)

    @server.tool(annotations=STATUS_CHANGE)
    async def shadow_strategy(strategy_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Move a strategy to shadow: evaluated on a virtual portfolio, never traded.
        Without confirm=true returns a preview and changes nothing."""
        return await change_status(strategy_id, "shadow", "shadow", confirm)

    @server.tool(annotations=STATUS_CHANGE)
    async def retire_strategy(strategy_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Retire a strategy: it stops being ranked or evaluated.
        Without confirm=true returns a preview and changes nothing."""
        return await change_status(strategy_id, "retire", "retired", confirm)

    @server.tool(annotations=TICK)
    async def run_tick(
        confirm: Confirm = False,
        dry_run: Annotated[
            bool, Field(description="true (default): rank and log only, place no orders")
        ] = True,
        as_of: Annotated[date | None, Field(description="YYYY-MM-DD; default today")] = None,
        tickers: Annotated[
            list[str] | None, Field(description="override [production].universe")
        ] = None,
        asset_class: AssetClass | None = None,
    ) -> dict[str, Any]:
        """Queue one production tick (rank active strategies, then trade the winner).
        Dry run by default. Without confirm=true returns a preview and queues
        nothing. A real tick (dry_run=false) is refused unless the API reports
        the broker is paper/simulated."""
        body = _drop_none(
            {
                "dry_run": dry_run,
                "as_of": _iso(as_of),
                "tickers": tickers,
                "asset_class": asset_class,
            }
        )
        live = "not checked (dry run)"
        if not dry_run:
            live = await _broker_live_state(api)
        if not confirm:
            active = await call(api.get("/api/strategies", {"status": "active", "limit": 500}))
            warnings = []
            if not dry_run and live != "off":
                warnings.append(_live_refusal(live))
            if not active.get("items"):
                warnings.append("no active strategies: the tick will place no orders")
            return {
                "preview": True,
                "applied": False,
                "request": body,
                "universe": tickers or "server default ([production].universe)",
                "active_strategies": [s.get("id") for s in active.get("items", [])],
                "live_trading": live,
                "warnings": warnings,
                "next_step": CONFIRM_HINT,
            }
        if not dry_run and live != "off":
            raise ToolError(_live_refusal(live))
        job = await call(api.post("/api/ticks", body))
        return {"preview": False, "applied": True, "job": job}


async def _broker_live_state(api: ApiClient) -> str:
    try:
        info = await api.get(BROKER_STATUS_PATH)
    except ApiError:
        return "unknown"
    return live_trading_state(info)


def _live_refusal(state: str) -> str:
    if state == "on":
        return (
            "refusing a non-dry-run tick: the API's broker is not simulated or paper, "
            "so it trades real money. "
            "Run live ticks from the CLI or UI, not through MCP."
        )
    return (
        "refusing a non-dry-run tick: the API did not report a simulated or paper broker, so "
        "MCP cannot rule out live trading. Use dry_run=true, or the CLI/UI."
    )


# --- resources --------------------------------------------------------------------


def _register_resources(server: MCPServer, api: ApiClient, call) -> None:
    @server.resource(
        "stonks://portfolio/summary",
        name="portfolio_summary",
        description="Cash, total value and positions of the current portfolio",
        mime_type="application/json",
    )
    async def portfolio_summary() -> str:
        p = await call(api.get("/api/portfolio"))
        summary = {
            "taken_at": p.get("taken_at"),
            "tick_id": p.get("tick_id"),
            "cash": p.get("cash"),
            "positions_value": p.get("positions_value"),
            "total_value": p.get("total_value"),
            "positions": [
                {k: pos.get(k) for k in ("ticker", "quantity", "market_value", "weight")}
                for pos in p.get("positions", [])
            ],
        }
        return json.dumps(summary)


# --- entrypoint --------------------------------------------------------------------


def run_stdio(api: ApiClient, *, max_wait_seconds: float = 600.0) -> None:
    """Serve over stdio until the client disconnects (blocking)."""
    build_server(api, max_wait_seconds=max_wait_seconds).run("stdio")
