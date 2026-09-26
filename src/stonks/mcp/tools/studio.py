"""Strategy Studio tools: rule templates and schema, drafts, validation,
draft backtests / lab runs (jobs), and guarded register / enable / disable.

Code drafts (``kind='code'``) run Python inside the API server; the API
refuses them with 403 unless ``[api] allow_code_strategies`` is on, and MCP
cannot change that setting.
"""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from stonks.mcp.guards import draft_preview
from stonks.mcp.tools.common import (
    EDIT,
    GUARDED_CREATE,
    JOB,
    READ,
    STATUS_CHANGE,
    Confirm,
    IsoDate,
    Limit,
    ObjectiveName,
    Offset,
    RegisterConfirm,
    RegisterStrategy,
    RouteRead,
    SurvivalTestName,
    Tickers,
    ToolContext,
    TunerName,
    drop_none,
    iso,
    queue_lab_run,
    register_route_reads,
    seg,
)

ROUTE_READS: tuple[RouteRead, ...] = (
    RouteRead(
        "list_studio_templates",
        "/api/studio/templates",
        "Starter rule specs (id, title, description, spec) for building a rule draft.",
    ),
    RouteRead(
        "get_rule_schema",
        "/api/studio/schema",
        "JSON Schema of a Studio rule spec (version 1): indicators, entry/exit "
        "conditions, ranking. Use it to write a draft's spec.",
    ),
)

#: Prefix for the API's 403 on code drafts.
HINTS = {
    403: "Code strategies are disabled on the Stonks API server (an operator must set "
    "[api] allow_code_strategies = true; MCP cannot change it)"
}

DraftName = Annotated[str, Field(min_length=1, max_length=100, description="draft name")]
Spec = Annotated[
    dict[str, Any],
    Field(description="rule spec (see get_rule_schema); constructor params for code drafts"),
]
SourceCode = Annotated[
    str | None,
    Field(description="Python source of a code draft (API must allow code strategies)"),
]


def register(t: ToolContext) -> None:
    register_route_reads(t, ROUTE_READS)
    server = t.server

    def draft_path(draft_id: str, action: str = "") -> str:
        suffix = f"/{action}" if action else ""
        return f"/api/studio/drafts/{seg(draft_id)}{suffix}"

    @server.tool(annotations=READ)
    async def validate_rule_spec(spec: Spec) -> dict[str, Any]:
        """Validate a rule spec without saving it (no smoke run): valid + issues."""
        return await t.post("/api/studio/spec/validate", {"spec": spec})

    @server.tool(annotations=READ)
    async def list_drafts(limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Studio drafts (strategies being edited), with status and registered strategy."""
        return await t.get("/api/studio/drafts", {"limit": limit, "offset": offset}, hints=HINTS)

    @server.tool(annotations=READ)
    async def get_draft(draft_id: str) -> dict[str, Any]:
        """One Studio draft: spec, kind, status and its registered strategy's status."""
        return await t.get(draft_path(draft_id), hints=HINTS)

    @server.tool(annotations=JOB)
    async def create_draft(
        name: DraftName,
        spec: Spec | None = None,
        kind: Literal["rule", "code"] = "rule",
        source_code: SourceCode = None,
    ) -> dict[str, Any]:
        """Create a Studio draft (saved, not registered, never traded). Rule
        drafts take a spec; code drafts take source_code and are refused (403)
        unless the API allows code strategies."""
        body = drop_none({"name": name, "kind": kind, "spec": spec, "source_code": source_code})
        return await t.post("/api/studio/drafts", body, hints=HINTS)

    @server.tool(annotations=EDIT)
    async def update_draft(
        draft_id: str,
        name: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        spec: Annotated[dict[str, Any] | None, Field(description="replaces the spec")] = None,
        source_code: SourceCode = None,
    ) -> dict[str, Any]:
        """Overwrite fields of a draft (only the ones given). A registered
        strategy keeps the version it was registered with."""
        body = drop_none({"name": name, "spec": spec, "source_code": source_code})
        if not body:
            raise ToolError("update_draft needs at least one of name, spec, source_code")
        return await t.patch(draft_path(draft_id), body, hints=HINTS)

    @server.tool(annotations=JOB)
    async def validate_draft(
        draft_id: str,
        tickers: Annotated[
            list[str] | None,
            Field(max_length=20, description="lake tickers to smoke-run on; default sample data"),
        ] = None,
        as_of: IsoDate | None = None,
        bars: Annotated[int | None, Field(ge=1, le=250, description="bars to evaluate")] = None,
    ) -> dict[str, Any]:
        """Validate a draft and smoke-run it (on lake tickers or synthetic data).
        Saves nothing, but a code draft's Python runs inside the API server."""
        body = drop_none({"tickers": tickers, "as_of": iso(as_of), "bars": bars})
        return await t.post(draft_path(draft_id, "validate"), body, hints=HINTS)

    @server.tool(annotations=JOB)
    async def backtest_draft(
        draft_id: str,
        universe: Tickers,
        start: IsoDate,
        end: IsoDate,
        interval: str = "1d",
        initial_cash: Annotated[float, Field(gt=0)] = 10_000.0,
        threshold: float = 0.0,
        rebalance_every_bars: Annotated[int, Field(ge=1)] = 1,
        slippage_bps: Annotated[float, Field(ge=0)] = 0.0,
        fee_per_trade: Annotated[float, Field(ge=0)] = 0.0,
    ) -> dict[str, Any]:
        """Queue a backtest of a draft. Returns the job; wait_for_job gives the
        BacktestResult. Simulated only: never places real orders."""
        body = {
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
        return await t.post(draft_path(draft_id, "backtests"), body, hints=HINTS)

    @server.tool(annotations=JOB)
    async def lab_run_draft(
        draft_id: str,
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
        register_strategy: RegisterStrategy = False,
        confirm: RegisterConfirm = False,
    ) -> dict[str, Any]:
        """Queue tune -> fit -> survival suite for a draft (a rule draft's spec
        is fixed; a code draft is tuned). Returns the job; wait_for_job gives
        the verdict and survival reports. With register_strategy=true it needs
        confirm=true (preview otherwise), like register_draft."""
        body = drop_none(
            {
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
        return await queue_lab_run(t, draft_path(draft_id, "lab-runs"), body, confirm, HINTS)

    async def guarded(draft_id: str, action: str, confirm: bool) -> dict[str, Any]:
        draft = await t.get(draft_path(draft_id), hints=HINTS)
        if not confirm:
            strategy = None
            sid = draft.get("registered_strategy_id")
            if action != "register" and sid:
                strategy = await t.get(f"/api/strategies/{seg(sid)}")
            return draft_preview(draft, action, strategy)
        updated = await t.post(draft_path(draft_id, action), hints=HINTS)
        return {
            "preview": False,
            "applied": True,
            "action": action,
            "previous_strategy_status": draft.get("strategy_status"),
            "draft": updated,
        }

    @server.tool(annotations=GUARDED_CREATE)
    async def register_draft(draft_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Register a draft's strategy in shadow (virtual portfolio, never traded
        until enabled). Without confirm=true returns a preview and changes nothing."""
        return await guarded(draft_id, "register", confirm)

    @server.tool(annotations=STATUS_CHANGE)
    async def enable_draft(draft_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Promote a registered draft's strategy to active so production ticks rank
        and trade it. Without confirm=true returns a preview (survival results,
        warnings) and changes nothing."""
        return await guarded(draft_id, "enable", confirm)

    @server.tool(annotations=STATUS_CHANGE)
    async def disable_draft(draft_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Move a registered draft's strategy back to shadow (stops trading it).
        Without confirm=true returns a preview and changes nothing."""
        return await guarded(draft_id, "disable", confirm)
