"""Shared pieces of the MCP tool modules: annotations, parameter types,
the per-server :class:`ToolContext`, and the declarative table for
parameterless read routes."""

# No ``from __future__ import annotations``: tool signatures use closure
# values inside ``Annotated`` metadata, which must be evaluated at def time.

from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.client import ApiClient, ApiError, segment
from stonks.mcp.guards import lab_registration_preview

# --- annotations --------------------------------------------------------------

READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: Queues background research work (or creates a draft): never destructive.
JOB = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
#: Ingest reaches out to external market-data vendors via the API.
JOB_OPEN_WORLD = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
#: Overwrites a draft's fields (repeating the same edit is a no-op).
EDIT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
#: Guarded: changes what production trades; needs ``confirm=true``.
STATUS_CHANGE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
#: Guarded: registration creates a new strategy each time; needs ``confirm=true``.
GUARDED_CREATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
TICK = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

# --- shared parameter types ------------------------------------------------------

Limit = Annotated[int, Field(ge=1, le=500, description="page size")]
Offset = Annotated[int, Field(ge=0, description="rows to skip")]
Ticker = Annotated[str, Field(description="instrument id, e.g. AAPL.US or BTC-USD.CC")]
Tickers = Annotated[list[str], Field(min_length=1, description="instrument ids")]
IsoDate = Annotated[date, Field(description="YYYY-MM-DD")]
Since = Annotated[date | None, Field(description="YYYY-MM-DD; first day to include")]
Confirm = Annotated[
    bool,
    Field(description="must be true to apply; false (default) returns a preview only"),
]
StrategyStatus = Literal["active", "shadow", "retired"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
AssetClass = Literal["equity", "crypto", "commodity", "bond"]
TunerName = Literal["grid", "random"]
ObjectiveName = Literal["sharpe", "cagr", "final_return"]
SurvivalTestName = Literal[
    "oos", "period_stability", "perturbation", "drift", "runs_test", "permutation", "walk_forward"
]


Reason = Annotated[
    str | None,
    Field(
        max_length=2000,
        description="why (logged in the audit trail); required for demotions and overrides",
    ),
]
Override = Annotated[
    bool,
    Field(
        description="promote without a passing go-live check; needs a reason of at least "
        "20 characters"
    ),
]
#: Actor logged for status changes made through MCP.
MCP_ACTOR = "mcp"
#: Explanations for the governed status routes' refusals.
STATUS_HINTS: dict[int, str] = {
    409: "Promotion refused by the go-live gate; wait for a passing paper period, or pass "
    "override=true with a reason of at least 20 characters",
    422: "The change needs a reason (and an override reason of at least 20 characters)",
}


def status_body(reason: str | None, override: bool = False) -> dict[str, Any]:
    """Body of a governed status-change route (``StatusChangeRequest``)."""
    return drop_none({"reason": reason, "override": override or None, "actor": MCP_ACTOR})


RegisterStrategy = Annotated[
    bool,
    Field(
        description="register the result in shadow status whatever the verdict (needs confirm=true)"
    ),
]
RegisterIfPasses = Annotated[
    bool,
    Field(
        description="register the result in shadow only if every survival test passes "
        "(needs confirm=true)"
    ),
]
RegisterConfirm = Annotated[
    bool,
    Field(
        description="must be true with register_strategy / register_if_passes; otherwise a preview"
    ),
]
CostModelName = Literal["zero", "realistic"]
LabCostModel = Annotated[
    CostModelName | None,
    Field(description="transaction-cost preset (see list_cost_models); default [backtest.costs]"),
]
SurvivalPreset = Annotated[
    Literal["quick", "standard", "promotion"] | None,
    Field(
        description="named survival suite when survival_tests is omitted (default: "
        "promotion when registering, else quick)"
    ),
]
Hypothesis = Annotated[
    str | None,
    Field(
        max_length=4000,
        description="the edge and who pays for it; recorded before tuning (trial ledger)",
    ),
]
Premortem = Annotated[
    str | None,
    Field(max_length=4000, description="how the strategy is expected to fail; recorded"),
]

#: HTTP status -> explanation prefixed to the API's error message.
Hints = Mapping[int, str]


def seg(value: str) -> str:
    """Path-safe id (see :func:`segment`), as a tool error when invalid.
    Every id interpolated into a URL goes through this."""
    try:
        return segment(value)
    except ApiError as exc:
        raise ToolError(str(exc)) from None


def iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def drop_none(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


def items(data: Any) -> Any:
    """Wrap a JSON list so a tool's structured output is always an object."""
    return {"items": data} if isinstance(data, list) else data


@dataclass(frozen=True)
class ToolContext:
    """What every tool module registers against: the server, the API
    client and the job-wait cap. API failures become tool errors whose
    text never contains the token."""

    server: MCPServer
    api: ApiClient
    max_wait_seconds: float = 600.0

    async def call(self, awaitable: Awaitable[Any], hints: Hints | None = None) -> Any:
        """``hints`` maps an HTTP status to a sentence put in front of the
        API's own message (e.g. why a 403 happened)."""
        try:
            return await awaitable
        except ApiError as exc:
            message = str(exc)
            if hints and exc.status in hints:
                message = f"{hints[exc.status]}. {message}"
            raise ToolError(self.api.redact(message)) from None

    async def get(
        self, path: str, params: dict[str, Any] | None = None, *, hints: Hints | None = None
    ) -> Any:
        return await self.call(self.api.get(path, params), hints)

    async def post(
        self, path: str, body: dict[str, Any] | None = None, *, hints: Hints | None = None
    ) -> Any:
        return await self.call(self.api.post(path, body), hints)

    async def patch(self, path: str, body: dict[str, Any], *, hints: Hints | None = None) -> Any:
        return await self.call(self.api.patch(path, body), hints)


async def queue_lab_run(
    t: ToolContext, path: str, body: dict[str, Any], confirm: bool, hints: Hints | None = None
) -> dict[str, Any]:
    """POST a lab run. Registering the result is a guarded write: with
    ``register_strategy`` / ``register_if_passes`` and no ``confirm`` it returns a preview and
    queues nothing; confirmed, it returns ``{"applied": True, "job": ...}``."""
    if not (body.get("register_strategy") or body.get("register_if_passes")):
        return await t.post(path, body, hints=hints)
    if not confirm:
        return lab_registration_preview(body)
    job = await t.post(path, body, hints=hints)
    return {"preview": False, "applied": True, "job": job}


# --- declarative parameterless reads ------------------------------------------------


@dataclass(frozen=True)
class RouteRead:
    """A read tool that is exactly one parameterless ``GET``: adding one
    is a row in a table. JSON lists come back as ``{"items": [...]}``."""

    name: str
    path: str
    description: str


def register_route_reads(t: ToolContext, routes: Iterable[RouteRead]) -> None:
    for route in routes:
        t.server.add_tool(
            _route_tool(t, route.path),
            name=route.name,
            description=route.description,
            annotations=READ,
        )


def _route_tool(t: ToolContext, path: str):
    async def tool() -> dict[str, Any]:
        return items(await t.get(path))

    return tool
