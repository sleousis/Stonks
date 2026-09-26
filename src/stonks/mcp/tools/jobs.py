"""Job tools: queue backtests, lab runs and ingests (research data only,
never orders), plus ``wait_for_job``."""

# No ``from __future__ import annotations`` (see common.py).

import time
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from stonks.mcp.tools.common import (
    JOB,
    JOB_OPEN_WORLD,
    READ,
    IsoDate,
    ObjectiveName,
    SurvivalTestName,
    Tickers,
    ToolContext,
    TunerName,
    drop_none,
    iso,
    seg,
)

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def strategy_ref(
    strategy_id: str | None, class_path: str | None, params: dict[str, Any] | None
) -> dict[str, Any]:
    if (strategy_id is None) == (class_path is None):
        raise ToolError("set exactly one of strategy_id or class_path")
    return drop_none({"strategy_id": strategy_id, "class_path": class_path, "params": params})


def register(t: ToolContext) -> None:
    server = t.server
    max_wait = t.max_wait_seconds

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
            "strategy": strategy_ref(strategy_id, class_path, params),
            "universe": universe,
            "start": iso(start),
            "end": iso(end),
            "interval": interval,
            "initial_cash": initial_cash,
            "threshold": threshold,
            "rebalance_every_bars": rebalance_every_bars,
            "slippage_bps": slippage_bps,
            "fee_per_trade": fee_per_trade,
        }
        return await t.post("/api/lab/backtests", body)

    @server.tool(annotations=JOB)
    async def run_lab(
        class_path: Annotated[str, Field(description="catalog class path to tune")],
        universe: Tickers,
        start: IsoDate,
        end: IsoDate,
        survival_tests: Annotated[
            list[SurvivalTestName] | None,
            Field(description="survival suite; server default when omitted"),
        ] = None,
        tuner: TunerName = "random",
        objective: ObjectiveName = "sharpe",
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
        body = drop_none(
            {
                "strategy": {"class_path": class_path},
                "universe": universe,
                "start": iso(start),
                "end": iso(end),
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
        return await t.post("/api/lab/runs", body)

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
        body = drop_none(
            {
                "kind": kind,
                "tickers": tickers,
                "exchange": exchange,
                "since": iso(since),
                "until": iso(until),
                "interval": interval,
            }
        )
        return await t.post("/api/ingest/runs", body)

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
        path = f"/api/jobs/{seg(job_id)}"
        deadline = time.monotonic() + min(timeout_seconds, max_wait)
        while True:
            job = await t.get(path)
            if job.get("status") in TERMINAL_JOB_STATUSES:
                return {"timed_out": False, "job": job}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"timed_out": True, "job": job}
            await anyio.sleep(min(poll_seconds, remaining))
