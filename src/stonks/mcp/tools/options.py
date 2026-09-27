"""Options research tools (roadmap 17.6): stored chains with Greeks, the
options strategies and structures, a structure's payoff (all read only),
and an options backtest (a research job that writes nothing). Nothing here
trades options."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from pydantic import Field

from stonks.mcp.tools.common import (
    JOB,
    READ,
    IsoDate,
    Ticker,
    ToolContext,
    drop_none,
    iso,
    items,
    seg,
)

Delta = Annotated[
    float | None, Field(gt=0.0, lt=1.0, description="absolute target delta, e.g. 0.30")
]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_option_underlyings() -> dict[str, Any]:
        """Underlyings with stored option chains: first and last day, days,
        contracts and sources. synthetic=true means generated chains, fine
        for trying the tools but never evidence."""
        return items(await t.get("/api/options/underlyings"))

    @server.tool(annotations=READ)
    async def get_option_chain(
        underlying: Ticker,
        as_of: Annotated[
            IsoDate | None, Field(description="the last stored day on or before it; default latest")
        ] = None,
        expiry: Annotated[
            IsoDate | None, Field(description="default: the expiry nearest 30 days out")
        ] = None,
    ) -> dict[str, Any]:
        """One expiry of a stored option chain with our implied vol and
        Greeks, calls and puts by strike. Theta is money per share per day,
        vega money per share per vol point."""
        return await t.get(
            f"/api/options/chains/{seg(underlying)}",
            params=drop_none({"as_of": iso(as_of), "expiry": iso(expiry)}),
        )

    @server.tool(annotations=READ)
    async def list_option_strategies() -> dict[str, Any]:
        """The options strategy catalog: each strategy's hypothesis, the
        structures it opens and its parameters."""
        return items(await t.get("/api/options/strategies"))

    @server.tool(annotations=READ)
    async def list_option_structures() -> dict[str, Any]:
        """Structures a payoff can be drawn for, with the parameters each
        reads (dte, delta, long_delta, short_delta, wing_delta)."""
        return items(await t.get("/api/options/structures"))

    @server.tool(annotations=READ)
    async def get_option_payoff(
        underlying: Ticker,
        structure: Annotated[str, Field(description="a name from list_option_structures")],
        as_of: IsoDate | None = None,
        dte: Annotated[int, Field(ge=1, le=400, description="target days to expiry")] = 35,
        delta: Delta = None,
        long_delta: Delta = None,
        short_delta: Delta = None,
        wing_delta: Delta = None,
    ) -> dict[str, Any]:
        """One unit of a structure picked from a stored chain, its legs and
        its profit at expiry over a range of underlying prices, with max
        loss, max gain (null: no bound) and breakevens."""
        body = drop_none(
            {
                "underlying": underlying,
                "structure": structure,
                "as_of": iso(as_of),
                "dte": dte,
                "delta": delta,
                "long_delta": long_delta,
                "short_delta": short_delta,
                "wing_delta": wing_delta,
            }
        )
        return await t.post("/api/options/payoff", body)

    @server.tool(annotations=JOB)
    async def run_options_backtest(
        strategy: Annotated[str, Field(description="an id from list_option_strategies")],
        underlyings: Annotated[list[str], Field(min_length=1, max_length=10)],
        start: IsoDate,
        end: IsoDate,
        cash: Annotated[float, Field(gt=0)] = 100_000.0,
        params: Annotated[
            dict[str, Any] | None, Field(description="strategy parameters; defaults otherwise")
        ] = None,
        validation: Annotated[
            bool,
            Field(description="also run out of sample, deflated Sharpe and stress checks"),
        ] = True,
        trials: Annotated[
            int, Field(ge=1, description="trials run so far, for the deflated Sharpe")
        ] = 1,
    ) -> dict[str, Any]:
        """Queue an options backtest on the stored chains, with its
        validation checks and a verdict. Returns the job; use wait_for_job
        for the result. Research only: nothing trades options."""
        body = drop_none(
            {
                "strategy": strategy,
                "underlyings": underlyings,
                "start": iso(start),
                "end": iso(end),
                "cash": cash,
                "params": params,
                "validation": validation,
                "trials": trials,
            }
        )
        return await t.post("/api/options/backtests", body)
