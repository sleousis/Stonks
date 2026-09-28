"""The assistant's tools: the existing MCP tools, run in process as the
signed-in user (roadmap 20.4).

:class:`McpToolBridge` builds the same MCP server ``stonks mcp`` runs
(:func:`stonks.mcp.server.build_server`), but its :class:`ApiClient` talks
to the running FastAPI app through an in-process ASGI transport instead of
the network. A small wrapper puts the caller's principal on each request's
state (``scope["state"]["principal"]``), which
:func:`stonks.api.deps._resolve` uses as the request's principal. Only code
in this process can do that: no header or cookie sets it.

The principal is the signed-in user with the same role and scopes, but
``via="assistant"`` and no fresh second factor. So every route checks the
same permission and ownership as in the web app, and step-up routes answer
``step_up_required``: those actions stay in the web app.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import MutableMapping
from dataclasses import dataclass, replace
from typing import Any

import httpx2

from stonks.assistant.model import ToolSpec
from stonks.auth.principal import Principal

#: Sent as the bearer so the MCP client's write methods run. The principal
#: on the request state wins, so this string authenticates nothing.
IN_PROCESS_TOKEN = "assistant-in-process"
IN_PROCESS_URL = "http://127.0.0.1"

STEP_UP_MESSAGE = (
    "This action needs a fresh second factor, so the assistant cannot do it. "
    "Do it in the Stonks web app while signed in."
)
FORBIDDEN_MESSAGE = "You are not allowed to do this: your role or access does not permit it."


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    parameters: dict[str, Any]
    #: readOnlyHint: runs without asking the person.
    read_only: bool
    destructive: bool
    #: The tool takes ``confirm`` (a guarded write that previews without it).
    takes_confirm: bool

    def spec(self) -> ToolSpec:
        """What the model sees. ``confirm`` is hidden: only the person confirms."""
        params = dict(self.parameters)
        props = dict(params.get("properties") or {})
        if "confirm" in props:
            props.pop("confirm")
            params["properties"] = props
            required = [r for r in params.get("required") or [] if r != "confirm"]
            if "required" in params:
                params["required"] = required
        return ToolSpec(self.name, self.description, params)


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    content: Any = None
    error: str | None = None


class ToolBridge(ABC):
    """The tools an assistant turn may call."""

    @abstractmethod
    async def tools(self) -> list[ToolInfo]: ...

    @abstractmethod
    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""


def assistant_principal(principal: Principal) -> Principal:
    """The signed-in user as the assistant acts: same role and scopes, no
    fresh second factor, ``via="assistant"``."""
    return replace(principal, mfa_fresh=False, via="assistant", credential_id=None)


class _AsPrincipal:
    """ASGI wrapper that sets the request's principal (in process only)."""

    def __init__(self, app: Any, principal: Principal) -> None:
        self._app = app
        self._principal = principal

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            state = dict(scope.get("state") or {})
            state["principal"] = self._principal
            scope = {**scope, "state": state}
        await self._app(scope, receive, send)


def friendly_error(text: str) -> str:
    """A tool error the person can act on. Step-up and permission refusals
    point to the web app, and the MCP-specific token hints go away."""
    if "fresh second factor" in text or "step_up_required" in text:
        return STEP_UP_MESSAGE
    if "(403)" in text:
        return FORBIDDEN_MESSAGE
    return text.removeprefix("Error executing tool ").strip()


def _decode(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = [getattr(c, "text", "") for c in getattr(result, "content", []) or []]
    text = "\n".join(t for t in texts if t)
    try:
        return json.loads(text)
    except ValueError:
        return text


class McpToolBridge(ToolBridge):
    """The MCP tools over the in-process API, as ``principal``."""

    def __init__(self, app: Any, principal: Principal, *, timeout: float = 120.0) -> None:
        from stonks.mcp.client import ApiClient
        from stonks.mcp.server import build_server

        transport = httpx2.ASGITransport(app=_AsPrincipal(app, assistant_principal(principal)))
        self._api = ApiClient(
            IN_PROCESS_URL, token=IN_PROCESS_TOKEN, timeout=timeout, transport=transport
        )
        # a token limited to some MCP toolsets keeps the same limit here (23.8)
        self._server = build_server(
            self._api, max_wait_seconds=min(timeout, 60.0), toolsets=principal.toolsets
        )
        self._tools: list[ToolInfo] | None = None

    async def tools(self) -> list[ToolInfo]:
        if self._tools is None:
            from stonks.mcp.tools.common import ALIASES

            out: list[ToolInfo] = []
            for tool in await self._server.list_tools():
                if tool.name in ALIASES:
                    continue
                notes = tool.annotations
                schema = dict(tool.input_schema or {"type": "object", "properties": {}})
                out.append(
                    ToolInfo(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=schema,
                        read_only=bool(notes and notes.read_only_hint),
                        destructive=bool(notes and notes.destructive_hint),
                        takes_confirm="confirm" in (schema.get("properties") or {}),
                    )
                )
            self._tools = out
        return self._tools

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            result = await self._server.call_tool(name, arguments)
        except ToolError as exc:
            return ToolOutcome(ok=False, error=friendly_error(str(exc)))
        except Exception as exc:  # a tool crashed: report, never raise into the loop
            return ToolOutcome(ok=False, error=f"the tool failed ({type(exc).__name__})")
        if getattr(result, "is_error", False):
            return ToolOutcome(ok=False, error=friendly_error(str(_decode(result))))
        return ToolOutcome(ok=True, content=_decode(result))

    async def aclose(self) -> None:
        await self._api.aclose()
