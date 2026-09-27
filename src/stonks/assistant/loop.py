"""The assistant's agent loop (roadmap 20.4).

One turn: the system prompt and the conversation go to the model; its tool
calls run through the :class:`~stonks.assistant.tools.ToolBridge`; their
results go back to the model; until it answers without a tool call.

Safety (the model proposes, deterministic code decides):

- Only the tools of :mod:`stonks.assistant.catalog` are offered: a default
  set of about twenty, plus the categories the conversation turned on. A
  research-only :class:`~stonks.assistant.guard.Gate` (the setting, a
  freeze, the kill switch) offers read tools only.
- Tool results go back to the model marked as untrusted data, and the
  system prompt says never to follow instructions found in them.
- ``draft_order`` needs a ticker a resolver tool (``search_instruments``)
  returned in this conversation, never one from the model's own text, and
  gets a retry key from the loop.
- Writes that touch only research data run at once. Every write is counted
  (``write_check``): a burst freezes the assistant and nothing more runs.
- Every turn is recorded (model, prompt version, tool calls, drafts).
- Read tools (``readOnlyHint``) run at once.
- Any other tool never runs on the model's say-so. The loop stores a
  pending action (the model's ``confirm`` argument is dropped), sends a
  ``confirm_required`` event with the tool's own preview when it has one,
  and stops. The person approves or rejects it (:meth:`AgentLoop.decide`):
  approved, it runs with ``confirm=true`` when the tool takes it. A new
  message while an action waits rejects that action.
- Limits: ``max_steps`` model calls per turn, ``max_tokens`` per call, a
  wall-clock ``timeout_seconds`` over the whole turn (tools included), and
  ``max_tool_result_chars`` of each tool result fed back.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio

from stonks.assistant import catalog
from stonks.assistant.guard import Gate
from stonks.assistant.model import (
    ChatMessage,
    ChatModel,
    ModelError,
    ModelTurn,
    ToolCall,
    ToolSpec,
)
from stonks.assistant.prompt import PROMPT_VERSION, system_prompt
from stonks.assistant.settings import AssistantConfig
from stonks.assistant.store import ConversationStore, PendingAction
from stonks.assistant.tools import ToolBridge, ToolInfo, ToolOutcome
from stonks.logging import get_logger

_log = get_logger("stonks.assistant.loop")

EventKind = Literal["text", "tool_call", "tool_result", "confirm_required", "done", "error"]

DECLINED = "The person declined this action. It was not run."
MOVED_ON = "The person sent a new message instead of confirming. The action was not run."
SKIPPED = "Not run: an earlier action in this step waits for the person's confirmation."
RESEARCH_ONLY = "Not run: the assistant is research only right now, so it cannot change anything."
UNRESOLVED = (
    "Not run: resolve the ticker with search_instruments first. A draft may only name an "
    "instrument that tool returned in this conversation."
)
TITLE_CHARS = 60

#: Checks one more write; returns why it is refused (a burst), or None.
WriteCheck = Callable[[], "str | None"]

_META_SPECS = (
    ToolSpec(
        catalog.LIST_CATEGORIES,
        "List the tool categories you can turn on, with the tools in each.",
        {"type": "object", "properties": {}},
    ),
    ToolSpec(
        catalog.ENABLE_CATEGORY,
        "Turn on one tool category for the rest of this conversation.",
        {
            "type": "object",
            "properties": {"category": {"type": "string", "enum": sorted(catalog.CATEGORIES)}},
            "required": ["category"],
        },
    ),
)


@dataclass(frozen=True)
class AssistantEvent:
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict[str, Any])


class ActionConflict(RuntimeError):
    """The action was already approved or rejected."""


def _clip(value: Any, limit: int) -> tuple[str, bool]:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return text, False
    return text[:limit] + " ...(truncated)", True


class AgentLoop:
    def __init__(
        self,
        model: ChatModel,
        bridge: ToolBridge,
        store: ConversationStore,
        config: AssistantConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        gate: Gate | None = None,
        write_check: WriteCheck | None = None,
        owner_id: str = "",
    ) -> None:
        self._model = model
        self._bridge = bridge
        self._store = store
        self._config = config
        self._clock = clock
        self._gate = gate or Gate(research_only=False, order_tools=False)
        self._write_check = write_check or (lambda: None)
        self._owner_id = owner_id
        self._trace: list[dict[str, Any]] = []
        self._draft_ids: list[str] = []

    # ---- entry points ------------------------------------------------------

    async def send(self, conversation_id: str, text: str) -> AsyncIterator[AssistantEvent]:
        """A person's message, then the assistant's turn."""
        deadline = self._clock() + self._config.timeout_seconds
        for action in self._store.pending(conversation_id):
            if self._store.claim(action.id, "rejected"):
                self._tool_message(conversation_id, action, {"ok": False, "error": MOVED_ON})
                self._store.finish(action.id, "rejected", {"error": MOVED_ON})
        self._store.add_message(conversation_id, ChatMessage(role="user", content=text))
        self._store.set_title_if_empty(conversation_id, text[:TITLE_CHARS])
        async for event in self._traced(conversation_id, self._steps(conversation_id, deadline)):
            yield event

    async def decide(
        self, conversation_id: str, action: PendingAction, approve: bool
    ) -> AsyncIterator[AssistantEvent]:
        """Run (approve) or drop (reject) a pending action, then continue."""
        async for event in self._traced(
            conversation_id, self._decide(conversation_id, action, approve)
        ):
            yield event

    async def _decide(
        self, conversation_id: str, action: PendingAction, approve: bool
    ) -> AsyncIterator[AssistantEvent]:
        deadline = self._clock() + self._config.timeout_seconds
        if not self._store.claim(action.id, "approved" if approve else "rejected"):
            raise ActionConflict(f"action {action.id} was already decided")
        refusal = None
        if approve:
            refusal = RESEARCH_ONLY if self._gate.research_only else self._write_check()
        if refusal is not None:
            self._tool_message(conversation_id, action, {"ok": False, "error": refusal})
            self._store.finish(action.id, "failed", {"error": refusal})
            yield AssistantEvent(
                "tool_result",
                {
                    "id": action.tool_call_id,
                    "name": action.tool_name,
                    "ok": False,
                    "error": refusal,
                },
            )
        elif not approve:
            self._tool_message(conversation_id, action, {"ok": False, "error": DECLINED})
            self._store.finish(action.id, "rejected", {"error": DECLINED})
            yield AssistantEvent(
                "tool_result",
                {
                    "id": action.tool_call_id,
                    "name": action.tool_name,
                    "ok": False,
                    "error": DECLINED,
                },
            )
        else:
            info = await self._tool(action.tool_name)
            arguments = dict(action.arguments)
            if info is not None and info.takes_confirm:
                arguments["confirm"] = True
            outcome = await self._run_tool(action.tool_name, arguments, deadline)
            self._store.finish(action.id, "done" if outcome.ok else "failed", _payload(outcome))
            self._tool_message(conversation_id, action, _payload(outcome))
            self._note(action.tool_name, arguments, outcome, confirmed=True)
            yield self._result_event(action.tool_call_id, action.tool_name, outcome)
        async for event in self._steps(conversation_id, deadline):
            yield event

    # ---- the loop --------------------------------------------------------------

    async def _steps(self, conversation_id: str, deadline: float) -> AsyncIterator[AssistantEvent]:
        cfg = self._config
        try:
            all_infos = {i.name: i for i in await self._bridge.tools()}
        except Exception as exc:
            _log.warning("assistant.tools_failed", error=type(exc).__name__)
            yield AssistantEvent("error", {"code": "tools_unavailable", "message": "tools failed"})
            yield self._done(conversation_id, 0)
            return
        infos = self._offered(conversation_id, all_infos)
        for step in range(1, cfg.max_steps + 1):
            specs = [*(i.spec() for i in infos.values()), *_META_SPECS]
            turn: ModelTurn | None = None
            try:
                async for chunk in self._model_chunks(conversation_id, specs, deadline):
                    if isinstance(chunk, ModelTurn):
                        turn = chunk
                    else:
                        yield AssistantEvent("text", {"delta": chunk})
            except TimeoutError:
                yield self._stop(conversation_id, "timeout", "The assistant ran out of time.")
                yield self._done(conversation_id, step)
                return
            except ModelError as exc:
                yield AssistantEvent("error", {"code": "model_error", "message": str(exc)})
                yield self._done(conversation_id, step)
                return
            assert turn is not None
            self._store.add_message(
                conversation_id,
                ChatMessage(role="assistant", content=turn.text, tool_calls=turn.tool_calls),
            )
            if not turn.tool_calls:
                yield self._done(conversation_id, step)
                return
            paused: str | None = None
            for tool_call in turn.tool_calls:
                if paused is not None:
                    self._answer(conversation_id, tool_call, {"ok": False, "error": SKIPPED})
                    continue
                async for event in self._handle_call(conversation_id, tool_call, infos, deadline):
                    if event.kind == "confirm_required":
                        paused = event.data["action_id"]
                    yield event
                if tool_call.name == catalog.ENABLE_CATEGORY:
                    infos = self._offered(conversation_id, all_infos)
                if self._clock() >= deadline:
                    break
            if paused is not None:
                yield self._done(conversation_id, step, pending=paused)
                return
            if self._clock() >= deadline:
                yield self._stop(conversation_id, "timeout", "The assistant ran out of time.")
                yield self._done(conversation_id, step)
                return
        yield self._stop(
            conversation_id,
            "max_steps",
            f"I stopped after {cfg.max_steps} steps. Ask again to continue.",
        )
        yield self._done(conversation_id, cfg.max_steps)

    async def _model_chunks(
        self, conversation_id: str, specs: list[Any], deadline: float
    ) -> AsyncIterator[str | ModelTurn]:
        messages = [
            ChatMessage(role="system", content=system_prompt()),
            *self._history(conversation_id),
        ]
        stream = self._model.stream(
            messages,
            specs,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
        ).__aiter__()
        try:
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    with anyio.fail_after(remaining):
                        chunk = await stream.__anext__()
                except StopAsyncIteration:
                    return
                yield chunk
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _handle_call(
        self,
        conversation_id: str,
        tool_call: ToolCall,
        infos: dict[str, ToolInfo],
        deadline: float,
    ) -> AsyncIterator[AssistantEvent]:
        name = tool_call.name
        info = infos.get(name)
        arguments = {k: v for k, v in tool_call.arguments.items() if k != "confirm"}
        writes = info is not None and not info.read_only
        runs_at_once = info is not None and (info.read_only or name in catalog.RESEARCH_WRITES)
        yield AssistantEvent(
            "tool_call",
            {
                "id": tool_call.id,
                "name": name,
                "arguments": arguments,
                "needs_confirmation": writes and not runs_at_once,
            },
        )
        refusal: str | None = None
        if name in (catalog.LIST_CATEGORIES, catalog.ENABLE_CATEGORY):
            outcome = self._meta(conversation_id, name, arguments)
        elif info is None:
            outcome = ToolOutcome(ok=False, error=f"the tool {name!r} is not available here")
        elif "_raw" in arguments:
            outcome = ToolOutcome(ok=False, error="the tool arguments were not valid JSON")
        elif writes and self._gate.research_only:
            outcome = ToolOutcome(ok=False, error=RESEARCH_ONLY)
        elif name == "draft_order" and not self._resolved(conversation_id, arguments):
            outcome = ToolOutcome(ok=False, error=UNRESOLVED)
        elif writes and (refusal := self._write_check()) is not None:
            outcome = ToolOutcome(ok=False, error=refusal)
        elif runs_at_once:
            if name == "draft_order":
                arguments["retry_key"] = f"{conversation_id}:{tool_call.id}"
            outcome = await self._run_tool(info.name, arguments, deadline)
            if writes:
                action = self._store.add_action(conversation_id, tool_call.id, name, arguments)
                status = "done" if outcome.ok else "failed"
                self._store.finish(action.id, status, _payload(outcome))
        else:
            preview: Any = None
            if info.destructive and info.takes_confirm:
                shown = await self._run_tool(info.name, {**arguments, "confirm": False}, deadline)
                if not shown.ok:
                    self._answer(conversation_id, tool_call, _payload(shown))
                    self._note(name, arguments, shown)
                    yield self._result_event(tool_call.id, name, shown)
                    return
                preview = shown.content
            action = self._store.add_action(conversation_id, tool_call.id, info.name, arguments)
            self._trace.append({"tool": name, "arguments": arguments, "pending_action": action.id})
            text, clipped = _clip(preview, self._config.max_tool_result_chars)
            yield AssistantEvent(
                "confirm_required",
                {
                    "action_id": action.id,
                    "tool": info.name,
                    "description": info.description,
                    "arguments": arguments,
                    "preview": text if clipped else preview,
                },
            )
            return
        self._answer(conversation_id, tool_call, _payload(outcome))
        self._note(name, arguments, outcome)
        yield self._result_event(tool_call.id, name, outcome)

    # ---- the envelope ------------------------------------------------------------

    def _offered(self, conversation_id: str, infos: dict[str, ToolInfo]) -> dict[str, ToolInfo]:
        names = catalog.offered(
            self._store.categories(conversation_id), order_tools=self._gate.order_tools
        )
        return {
            n: i
            for n, i in infos.items()
            if n in names and (i.read_only or not self._gate.research_only)
        }

    def _meta(self, conversation_id: str, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name == catalog.ENABLE_CATEGORY:
            category = str(arguments.get("category", ""))
            if category not in catalog.CATEGORIES:
                return ToolOutcome(ok=False, error=f"no tool category {category!r}")
            enabled = self._store.enable_category(conversation_id, category)
            return ToolOutcome(ok=True, content={"enabled": list(enabled)})
        enabled = set(self._store.categories(conversation_id))
        return ToolOutcome(
            ok=True,
            content={
                "categories": [
                    {"name": c, "about": about, "on": c in enabled, "tools": sorted(tools)}
                    for c, (about, tools) in sorted(catalog.CATEGORIES.items())
                ]
            },
        )

    def _resolved(self, conversation_id: str, arguments: dict[str, Any]) -> bool:
        """Whether the draft's ticker came from a resolver tool's result."""
        ticker = str(arguments.get("ticker", "")).strip().upper()
        if not ticker:
            return False
        for message in self._store.messages(conversation_id):
            if message.role != "tool" or message.tool_name not in catalog.RESOLVERS:
                continue
            try:
                payload = json.loads(message.content)
            except ValueError:
                continue
            result = payload.get("result") if isinstance(payload, dict) else None
            if payload.get("ok") and ticker in _instrument_ids(result):
                return True
        return False

    def _note(
        self,
        name: str,
        arguments: dict[str, Any],
        outcome: ToolOutcome,
        *,
        confirmed: bool = False,
    ) -> None:
        entry: dict[str, Any] = {"tool": name, "arguments": arguments, "ok": outcome.ok}
        if confirmed:
            entry["confirmed"] = True
        if outcome.ok:
            entry["result"], _ = _clip(outcome.content, 2000)
            content = outcome.content
            if name == "draft_order" and isinstance(content, dict) and content.get("id"):
                self._draft_ids.append(str(content["id"]))
        else:
            entry["error"] = outcome.error
        self._trace.append(entry)

    async def _traced(
        self, conversation_id: str, events: AsyncIterator[AssistantEvent]
    ) -> AsyncIterator[AssistantEvent]:
        """Record the turn: model, prompt version, tool calls, drafts."""
        turn_id = self._store.start_turn(
            conversation_id, self._owner_id, self._config.model, PROMPT_VERSION
        )
        status, steps = "failed", 0
        try:
            async for event in events:
                if event.kind == "done":
                    steps = int(event.data.get("steps") or 0)
                    if status in ("failed", "running"):
                        status = "paused" if event.data.get("pending_action_id") else "done"
                elif event.kind == "error":
                    status = str(event.data.get("code") or "error")
                yield event
        finally:
            self._store.finish_turn(
                turn_id,
                status=status,
                steps=steps,
                trace=self._trace,
                draft_ids=self._draft_ids,
            )
            self._trace, self._draft_ids = [], []

    async def _run_tool(self, name: str, arguments: dict[str, Any], deadline: float) -> ToolOutcome:
        remaining = deadline - self._clock()
        if remaining <= 0:
            return ToolOutcome(ok=False, error="the assistant ran out of time")
        try:
            with anyio.fail_after(remaining):
                return await self._bridge.call(name, arguments)
        except TimeoutError:
            return ToolOutcome(ok=False, error="the tool did not answer in time")

    async def _tool(self, name: str) -> ToolInfo | None:
        for info in await self._bridge.tools():
            if info.name == name:
                return info
        return None

    # ---- messages ----------------------------------------------------------------

    def _history(self, conversation_id: str) -> list[ChatMessage]:
        stored = self._store.messages(conversation_id, last=self._config.max_conversation_messages)
        # A cut history must not start with tool answers whose call was cut off.
        while stored and stored[0].role == "tool":
            stored = stored[1:]
        return [_untrusted(m.chat()) for m in stored]

    def _answer(self, conversation_id: str, tool_call: ToolCall, payload: dict[str, Any]) -> None:
        text, _ = _clip(payload, self._config.max_tool_result_chars)
        self._store.add_message(
            conversation_id,
            ChatMessage(role="tool", content=text, tool_call_id=tool_call.id, name=tool_call.name),
        )

    def _tool_message(
        self, conversation_id: str, action: PendingAction, payload: dict[str, Any]
    ) -> None:
        self._answer(conversation_id, ToolCall(action.tool_call_id, action.tool_name), payload)

    def _result_event(self, call_id: str, name: str, outcome: ToolOutcome) -> AssistantEvent:
        data: dict[str, Any] = {"id": call_id, "name": name, "ok": outcome.ok}
        if outcome.ok:
            text, clipped = _clip(outcome.content, self._config.max_tool_result_chars)
            data["result"] = text if clipped else outcome.content
        else:
            data["error"] = outcome.error
        return AssistantEvent("tool_result", data)

    def _stop(self, conversation_id: str, code: str, message: str) -> AssistantEvent:
        self._store.add_message(conversation_id, ChatMessage(role="assistant", content=message))
        return AssistantEvent("error", {"code": code, "message": message})

    @staticmethod
    def _done(conversation_id: str, steps: int, pending: str | None = None) -> AssistantEvent:
        return AssistantEvent(
            "done",
            {"conversation_id": conversation_id, "steps": steps, "pending_action_id": pending},
        )


def _payload(outcome: ToolOutcome) -> dict[str, Any]:
    if outcome.ok:
        return {"ok": True, "result": outcome.content}
    return {"ok": False, "error": outcome.error}


def _untrusted(message: ChatMessage) -> ChatMessage:
    """A tool result as the model sees it: marked as untrusted data."""
    if message.role != "tool":
        return message
    return ChatMessage(
        role="tool",
        content=(
            f'<tool_result name="{message.name}" trust="untrusted">\n'
            f"{message.content}\n</tool_result>"
        ),
        tool_call_id=message.tool_call_id,
        name=message.name,
    )


def _instrument_ids(result: Any) -> set[str]:
    """Every instrument id (an ``id`` or ``ticker`` field) in a tool result."""
    found: set[str] = set()
    stack: list[Any] = [result]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("id", "ticker"):
                value = item.get(key)
                if isinstance(value, str):
                    found.add(value.upper())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found
