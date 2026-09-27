"""AssistantService: the in-app AI assistant (roadmap 20.4).

Conversations belong to one person (another person's reads as missing).
A turn runs the :class:`~stonks.assistant.loop.AgentLoop` with the
configured model and the MCP tools, as the signed-in person (see
:mod:`stonks.assistant.tools`). The service yields plain
:class:`AssistantEvent` values: the REST API streams them as server-sent
events, and :meth:`AssistantService.collect` gathers them for callers that
do not stream.

Checks (permission, configured, ownership, action state) run when a turn
is opened, before anything streams, so they answer as normal problems.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.app.context import AppContext
from stonks.app.errors import ConfigurationError, ConflictError, NotFoundError
from stonks.app.pagination import Page
from stonks.assistant.loop import ActionConflict, AgentLoop, AssistantEvent, EventKind
from stonks.assistant.model import ChatModel, OpenAICompatibleModel
from stonks.assistant.settings import AssistantConfig
from stonks.assistant.store import (
    ActionNotFound,
    ConversationNotFound,
    ConversationStore,
    PendingAction,
    StoredMessage,
)
from stonks.assistant.tools import McpToolBridge, ToolBridge
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.logging import get_logger

_log = get_logger("stonks.app.assistant")

ModelFactory = Callable[[AssistantConfig], ChatModel]
#: ``(ASGI app, principal) -> bridge``: the tools a turn may call.
BridgeFactory = Callable[[Any, Principal], ToolBridge]

NOT_CONFIGURED = (
    "the assistant is off: set [assistant] base_url to an OpenAI-compatible endpoint "
    "(Ollama, vLLM or a llama.cpp server)"
)


# ---- views and requests -----------------------------------------------------------


class AssistantStatusView(BaseModel):
    enabled: bool = Field(description="True when a model endpoint is configured.")
    model: str | None = Field(description="The model name, when enabled.")
    max_steps: int
    max_tokens: int
    timeout_seconds: float


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=80)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8000)


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool = Field(description="true runs the action, false drops it.")


class ConversationView(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ToolCallView(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class MessageView(BaseModel):
    id: int
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCallView] = Field(default_factory=list[ToolCallView])
    tool_call_id: str | None = None
    tool_name: str | None = None
    created_at: datetime


class PendingActionView(BaseModel):
    id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: Literal["pending", "approved", "rejected", "done", "failed"]
    created_at: datetime


class ConversationDetailView(ConversationView):
    messages: list[MessageView]
    pending_actions: list[PendingActionView] = Field(
        description="Write actions waiting for you to approve or reject."
    )


class AssistantEventView(BaseModel):
    """One server-sent event of a turn. ``kind`` is also the SSE event name.

    - ``text``: ``{delta}``, a piece of the answer
    - ``tool_call``: ``{id, name, arguments, needs_confirmation}``
    - ``tool_result``: ``{id, name, ok, result | error}``
    - ``confirm_required``: ``{action_id, tool, description, arguments, preview}``
    - ``error``: ``{code, message}`` (``model_error``, ``timeout``, ``max_steps``, ...)
    - ``done``: ``{conversation_id, steps, pending_action_id}``, always last
    """

    kind: EventKind
    data: dict[str, Any]


def event_view(event: AssistantEvent) -> AssistantEventView:
    return AssistantEventView(kind=event.kind, data=event.data)


def _conversation_view(c: Any) -> ConversationView:
    return ConversationView(
        id=c.id,
        title=c.title,
        created_at=datetime.fromisoformat(c.created_at),
        updated_at=datetime.fromisoformat(c.updated_at),
    )


def _message_view(m: StoredMessage) -> MessageView:
    return MessageView(
        id=m.id,
        role=m.role,  # type: ignore[arg-type]  # stored rows are never system
        content=m.content,
        tool_calls=[
            ToolCallView(id=c.id, name=c.name, arguments=c.arguments) for c in m.tool_calls
        ],
        tool_call_id=m.tool_call_id,
        tool_name=m.tool_name,
        created_at=datetime.fromisoformat(m.created_at),
    )


def _action_view(a: PendingAction) -> PendingActionView:
    return PendingActionView(
        id=a.id,
        tool_call_id=a.tool_call_id,
        tool_name=a.tool_name,
        arguments=a.arguments,
        status=a.status,
        created_at=datetime.fromisoformat(a.created_at),
    )


def default_model(config: AssistantConfig) -> ChatModel:
    assert config.base_url is not None
    return OpenAICompatibleModel(
        config.base_url,
        config.model,
        api_key=AssistantConfig.api_key(),
        timeout=config.timeout_seconds,
    )


def default_bridge(app: Any, principal: Principal) -> ToolBridge:
    return McpToolBridge(app, principal)


class AssistantService:
    def __init__(
        self,
        context: AppContext,
        *,
        model_factory: ModelFactory | None = None,
        bridge_factory: BridgeFactory | None = None,
    ) -> None:
        self._ctx = context
        self._store = ConversationStore(context.state)
        self.model_factory: ModelFactory = model_factory or default_model
        self.bridge_factory: BridgeFactory = bridge_factory or default_bridge

    @property
    def config(self) -> AssistantConfig:
        return self._ctx.settings.assistant

    # ---- reads and conversation admin ----------------------------------------

    def status(self, principal: Principal) -> AssistantStatusView:
        require(principal, Permission.READ)
        cfg = self.config
        return AssistantStatusView(
            enabled=cfg.enabled,
            model=cfg.model if cfg.enabled else None,
            max_steps=cfg.max_steps,
            max_tokens=cfg.max_tokens,
            timeout_seconds=cfg.timeout_seconds,
        )

    def list(self, principal: Principal, *, limit: int, offset: int) -> Page[ConversationView]:
        require(principal, Permission.READ)
        rows, total = self._store.list(principal.user_id, limit=limit, offset=offset)
        return Page[ConversationView](
            items=[_conversation_view(c) for c in rows], total=total, limit=limit, offset=offset
        )

    def create(self, principal: Principal, body: ConversationCreate) -> ConversationView:
        require(principal, Permission.READ)
        return _conversation_view(self._store.create(principal.user_id, body.title))

    def get(self, principal: Principal, conversation_id: str) -> ConversationDetailView:
        require(principal, Permission.READ)
        conv = self._owned(principal, conversation_id)
        base = _conversation_view(conv)
        return ConversationDetailView(
            **base.model_dump(),
            messages=[_message_view(m) for m in self._store.messages(conv.id)],
            pending_actions=[_action_view(a) for a in self._store.pending(conv.id)],
        )

    def delete(self, principal: Principal, conversation_id: str) -> None:
        require(principal, Permission.READ)
        self._owned(principal, conversation_id)
        self._store.delete(principal.user_id, conversation_id)

    # ---- turns ------------------------------------------------------------------

    def open_message(
        self, principal: Principal, app: Any, conversation_id: str, body: MessageCreate
    ) -> AsyncIterator[AssistantEvent]:
        """Check everything, then return the turn's events (not started)."""
        require(principal, Permission.READ)
        self._configured()
        self._owned(principal, conversation_id)
        return self._run(principal, app, lambda loop: loop.send(conversation_id, body.content))

    def open_decision(
        self,
        principal: Principal,
        app: Any,
        conversation_id: str,
        action_id: str,
        body: ActionDecision,
    ) -> AsyncIterator[AssistantEvent]:
        require(principal, Permission.READ)
        self._configured()
        self._owned(principal, conversation_id)
        try:
            action = self._store.action(conversation_id, action_id)
        except ActionNotFound:
            raise NotFoundError(f"no action {action_id!r} in this conversation") from None
        if action.status != "pending":
            raise ConflictError(f"action {action_id} was already {action.status}")
        return self._run(
            principal, app, lambda loop: loop.decide(conversation_id, action, body.approve)
        )

    async def collect(self, events: AsyncIterator[AssistantEvent]) -> list[AssistantEvent]:
        """Every event of a turn, for callers that do not stream."""
        return [e async for e in events]

    async def _run(
        self,
        principal: Principal,
        app: Any,
        start: Callable[[AgentLoop], AsyncIterator[AssistantEvent]],
    ) -> AsyncIterator[AssistantEvent]:
        bridge = self.bridge_factory(app, principal)
        loop = AgentLoop(self.model_factory(self.config), bridge, self._store, self.config)
        try:
            async for event in start(loop):
                yield event
        except ActionConflict as exc:
            yield AssistantEvent("error", {"code": "conflict", "message": str(exc)})
            yield AssistantEvent("done", {"steps": 0, "pending_action_id": None})
        finally:
            await bridge.aclose()
            _log.info("assistant.turn_end", user_id=principal.user_id)

    def _configured(self) -> None:
        if not self.config.enabled:
            raise ConfigurationError(NOT_CONFIGURED)

    def _owned(self, principal: Principal, conversation_id: str) -> Any:
        try:
            return self._store.get(principal.user_id, conversation_id)
        except ConversationNotFound:
            raise NotFoundError(f"no conversation {conversation_id!r}") from None
