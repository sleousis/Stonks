"""Model versions (roadmap 22.6): reads, the swap check, and the guarded
writes. ``retrain_models``, ``swap_model_version`` and
``reject_model_version`` need ``confirm=true``; without it they return a
preview and send nothing mutating."""

# No ``from __future__ import annotations`` (see common.py).

from datetime import date
from typing import Annotated, Any

from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import (
    JOB,
    READ,
    STATUS_CHANGE,
    Confirm,
    Limit,
    Offset,
    Reason,
    ToolContext,
    drop_none,
    iso,
    items,
    seg,
)

Version = Annotated[int, Field(ge=1, description="the model version number")]
SwapOverride = Annotated[
    bool,
    Field(
        description="swap without a passing swap check; needs a reason of at least 20 characters"
    ),
]

#: Explanations for the governed version routes' refusals.
SWAP_HINTS: dict[int, str] = {
    409: "Swap refused by the swap check; wait for more model book days, or pass "
    "override=true with a reason of at least 20 characters",
    422: "The change needs a reason (and an override reason of at least 20 characters)",
}


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_model_versions(
        strategy_id: str, limit: Limit = 100, offset: Offset = 0
    ) -> dict[str, Any]:
        """A strategy's model versions, oldest first: the live one, candidates
        running as model books beside it, and archived, rejected or failed fits."""
        return await t.get(
            f"/api/strategies/{seg(strategy_id)}/versions", {"limit": limit, "offset": offset}
        )

    @server.tool(annotations=READ)
    async def get_model_version_history(strategy_id: str) -> dict[str, Any]:
        """A strategy's append-only version log (baseline, candidate, swap,
        reject, supersede, fail), with actor, reason and swap check result."""
        return items(await t.get(f"/api/strategies/{seg(strategy_id)}/versions/history"))

    @server.tool(annotations=READ)
    async def list_model_candidates(limit: Limit = 100, offset: Offset = 0) -> dict[str, Any]:
        """Every candidate model version running as a model book, across strategies."""
        return await t.get("/api/model-versions/candidates", {"limit": limit, "offset": offset})

    @server.tool(annotations=READ)
    async def check_model_swap(strategy_id: str, version: Version) -> dict[str, Any]:
        """The swap check of a candidate against the live version: model
        book days, drawdown and return against the live model over the same
        days, each with value, limit and pass or fail. Read it before
        swap_model_version: an override skips exactly these checks."""
        return await t.get(f"/api/strategies/{seg(strategy_id)}/versions/{int(version)}/check")

    @server.tool(annotations=READ)
    async def get_model_calibration(strategy_id: str, version: Version) -> dict[str, Any]:
        """Live calibration of a classifier version's probability forecasts:
        Brier score against the base rate, skill, expected calibration error
        and the reliability table. Empty for a model with no forecasts."""
        return await t.get(
            f"/api/strategies/{seg(strategy_id)}/versions/{int(version)}/calibration"
        )

    @server.tool(annotations=JOB)
    async def retrain_models(
        confirm: Confirm = False,
        strategy_ids: Annotated[
            list[str] | None,
            Field(description="refit only these (default: every strategy that learns from data)"),
        ] = None,
        as_of: Annotated[
            date | None, Field(description="YYYY-MM-DD, last day of the training window")
        ] = None,
        force: Annotated[bool, Field(description="refit even when a recent fit exists")] = False,
        tickers: Annotated[
            list[str] | None,
            Field(description="tickers to fit on when a strategy records no lab universe"),
        ] = None,
    ) -> dict[str, Any]:
        """Queue a retrain. Each fit becomes a candidate version that runs as
        a model book; nothing trades until a swap. Without confirm=true
        returns a preview and queues nothing."""
        body = drop_none(
            {
                "strategy_ids": strategy_ids,
                "as_of": iso(as_of),
                "force": force or None,
                "tickers": tickers,
            }
        )
        if not confirm:
            return {
                "preview": True,
                "applied": False,
                "request": body,
                "effect": "adds candidate versions; the live models keep trading",
                "next_step": CONFIRM_HINT,
            }
        job = await t.post("/api/model-versions/retrain", body)
        return {"preview": False, "applied": True, "job": job}

    @server.tool(annotations=STATUS_CHANGE)
    async def swap_model_version(
        strategy_id: str,
        version: Version,
        confirm: Confirm = False,
        reason: Reason = None,
        override: SwapOverride = False,
    ) -> dict[str, Any]:
        """Make a candidate version live, so the next tick trades it. Needs a
        passing swap check, or override=true with a reason of at least 20
        characters, and the change is audited. Without confirm=true returns
        the swap check as a preview and changes nothing."""
        base = f"/api/strategies/{seg(strategy_id)}/versions/{int(version)}"
        if not confirm:
            check = await t.get(f"{base}/check")
            warnings = [] if check.get("passed") or override else ["the swap check fails"]
            return {
                "preview": True,
                "applied": False,
                "swap_check": check,
                "reason": reason,
                "override": override,
                "warnings": warnings,
                "next_step": CONFIRM_HINT,
            }
        body = drop_none({"reason": reason, "override": override or None})
        swapped = await t.post(f"{base}/swap", body, hints=SWAP_HINTS)
        return {"preview": False, "applied": True, "version": swapped}

    @server.tool(annotations=STATUS_CHANGE)
    async def reject_model_version(
        strategy_id: str, version: Version, confirm: Confirm = False, reason: Reason = None
    ) -> dict[str, Any]:
        """Drop a candidate version: its model book stops and it can never
        swap in. Needs a reason. Without confirm=true returns a preview and
        changes nothing."""
        base = f"/api/strategies/{seg(strategy_id)}/versions"
        if not confirm:
            versions = await t.get(base, {"limit": 500})
            match = next(
                (v for v in versions.get("items", []) if v.get("version") == int(version)), None
            )
            return {
                "preview": True,
                "applied": False,
                "version": match,
                "reason": reason,
                "next_step": CONFIRM_HINT,
            }
        rejected = await t.post(
            f"{base}/{int(version)}/reject", drop_none({"reason": reason}), hints=SWAP_HINTS
        )
        return {"preview": False, "applied": True, "version": rejected}
