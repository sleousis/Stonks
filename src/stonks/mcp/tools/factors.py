"""Factor tools (roadmap 22.2, 22.3, 22.8): read the factor library, check
a formula, read factor values at a date (all read only), and queue a factor
tear sheet (a research job that writes nothing)."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from pydantic import Field

from stonks.mcp.tools.common import JOB, READ, IsoDate, ToolContext, drop_none, iso, seg

FactorRef = Annotated[
    str,
    Field(
        min_length=1,
        max_length=2000,
        description="a library factor id (e.g. mom_12_1, ROC20, piotroski_f) or a formula "
        "such as '$close / Ref($close, 20) - 1'",
    ),
]
Universe = Annotated[
    list[str] | None, Field(description="instrument ids; or name a stored universe_id")
]
UniverseId = Annotated[str | None, Field(description="a stored universe id (or give universe)")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_factors(
        family: Annotated[str | None, Field(description="e.g. momentum, value, kbar")] = None,
        set: Annotated[str | None, Field(description="alpha158, classic or fundamentals")] = None,
        kind: Literal["expression", "fundamental"] | None = None,
    ) -> dict[str, Any]:
        """The factor library: every factor with its family, direction,
        formula and hypothesis, plus the sets and families to filter by."""
        return await t.get(
            "/api/factors", params=drop_none({"family": family, "set": set, "kind": kind})
        )

    @server.tool(annotations=READ)
    async def get_factor(factor_id: str) -> dict[str, Any]:
        """One library factor: formula, family, direction, hypothesis and
        warm-up in bars."""
        return await t.get(f"/api/factors/{seg(factor_id)}")

    @server.tool(annotations=READ)
    async def check_factor_expression(
        expression: Annotated[str, Field(min_length=1, max_length=2000)],
    ) -> dict[str, Any]:
        """Check a formula in the factor expression language: whether it
        parses and is point in time (no future reads, no raw price levels
        that later splits would change), its canonical form and warm-up."""
        return await t.post("/api/factors/check", {"expression": expression})

    @server.tool(annotations=READ)
    async def get_factor_values(
        factor: FactorRef,
        as_of: IsoDate,
        universe: Universe = None,
        universe_id: UniverseId = None,
    ) -> dict[str, Any]:
        """Each universe name's factor value known at the close of as_of,
        ranked best first in the factor's direction."""
        body = drop_none(
            {
                "factor": factor,
                "as_of": iso(as_of),
                "universe": universe,
                "universe_id": universe_id,
            }
        )
        return await t.post("/api/factors/values", body)

    @server.tool(annotations=JOB)
    async def run_factor_tearsheet(
        factor: FactorRef,
        start: IsoDate,
        end: IsoDate,
        universe: Universe = None,
        universe_id: UniverseId = None,
        interval: str = "1d",
        horizons: Annotated[
            list[int] | None,
            Field(description="forward-return horizons in bars; default [1, 5, 21]"),
        ] = None,
        every_bars: Annotated[int, Field(ge=1, description="sample every N bars")] = 5,
        n_quantiles: Annotated[int, Field(ge=2, le=20)] = 5,
    ) -> dict[str, Any]:
        """Queue a factor tear sheet: IC per horizon and by sector, asset
        class and size, returns per quantile, factor alpha and beta, a
        monthly IC heatmap and turnover. Needs at least 10 tickers (else
        n/a). Returns the job; use wait_for_job for the result. Research
        only: writes nothing."""
        body = drop_none(
            {
                "factor": factor,
                "universe": universe,
                "universe_id": universe_id,
                "start": iso(start),
                "end": iso(end),
                "interval": interval,
                "horizons": horizons,
                "every_bars": every_bars,
                "n_quantiles": n_quantiles,
            }
        )
        return await t.post("/api/factors/tearsheets", body)
