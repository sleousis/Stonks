"""The assistant's agent loop (roadmap 20.4).

One turn: the system prompt and the conversation go to the model; its tool
calls run through the :class:`~stonks.assistant.tools.ToolBridge`; their
results go back to the model; until it answers without a tool call.

Safety:

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

from stonks.assistant.model import ChatMessage, ChatModel, ModelError, ModelTurn, ToolCall
from stonks.assistant.prompt import system_prompt
from stonks.assistant.settings import AssistantConfig
from stonks.assistant.store import ConversationStore, PendingAction
from stonks.assistant.tools import ToolBridge, ToolInfo, ToolOutcome
from stonks.logging import get_logger

_log = get_logger("stonks.assistant.loop")

EventKind = Literal["text", "tool_call", "tool_result", "confirm_required", "done", "error"]

DECLINED = "The person declined this action. It was not run."
MOVED_ON = "The person sent a new message instead of confirming. The action was not run."
SKIPPED = "Not run: an earlier action in this step waits for the person's confirmation."
TITLE_CHARS = 60


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
    ) -> None:
        self._model = model
        self._bridge = bridge
        self._store = store
        self._config = config
        self._clock = clock

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
        async for event in self._steps(conversation_id, deadline):
            yield event

    async def decide(
        self, conversation_id: str, action: PendingAction, approve: bool
    ) -> AsyncIterator[AssistantEvent]:
        """Run (approve) or drop (reject) a pending action, then continue."""
        deadline = self._clock() + self._config.timeout_seconds
        if not self._store.claim(action.id, "approved" if approve else "rejected"):
            raise ActionConflict(f"action {action.id} was already decided")
        if not approve:
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
            yield self._result_event(action.tool_call_id, action.tool_name, outcome)
        async for event in self._steps(conversation_id, deadline):
            yield event

    # ---- the loop --------------------------------------------------------------

    async def _steps(self, conversation_id: str, deadline: float) -> AsyncIterator[AssistantEvent]:
        cfg = self._config
        try:
            infos = {i.name: i for i in await self._bridge.tools()}
        except Exception as exc:
            _log.warning("assistant.tools_failed", error=type(exc).__name__)
            yield AssistantEvent("error", {"code": "tools_unavailable", "message": "tools failed"})
            yield self._done(conversation_id, 0)
            return
        specs = [i.spec() for i in infos.values()]
        for step in range(1, cfg.max_steps + 1):
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
        info = infos.get(tool_call.name)
        arguments = {k: v for k, v in tool_call.arguments.items() if k != "confirm"}
        needs_confirmation = info is not None and not info.read_only
        yield AssistantEvent(
            "tool_call",
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": arguments,
                "needs_confirmation": needs_confirmation,
            },
        )
        if info is None:
            outcome = ToolOutcome(ok=False, error=f"there is no tool named {tool_call.name!r}")
        elif "_raw" in arguments:
            outcome = ToolOutcome(ok=False, error="the tool arguments were not valid JSON")
        elif info.read_only:
            outcome = await self._run_tool(info.name, arguments, deadline)
        else:
            preview: Any = None
            if info.destructive and info.takes_confirm:
                shown = await self._run_tool(info.name, {**arguments, "confirm": False}, deadline)
                if not shown.ok:
                    self._answer(conversation_id, tool_call, _payload(shown))
                    yield self._result_event(tool_call.id, tool_call.name, shown)
                    return
                preview = shown.content
            action = self._store.add_action(conversation_id, tool_call.id, info.name, arguments)
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
        yield self._result_event(tool_call.id, tool_call.name, outcome)

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
        return [m.chat() for m in stored]

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
