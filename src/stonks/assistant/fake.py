"""A scripted :class:`ChatModel` for tests and local demos.

Each call replays the next scripted turn: text (streamed word by word)
and tool calls. It records what it was sent, so tests can check the
conversation the loop built. When the script runs out it answers
``"done"``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio

from stonks.assistant.model import (
    ChatMessage,
    ChatModel,
    ModelChunk,
    ModelError,
    ModelTurn,
    ToolCall,
    ToolSpec,
)


@dataclass(frozen=True)
class Script:
    """One scripted model turn."""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    #: Raise this instead of answering.
    error: str | None = None
    #: Sleep this long first (timeout tests).
    delay: float = 0.0


def call(name: str, arguments: dict[str, Any] | None = None, *, id: str | None = None) -> ToolCall:
    """A tool call for a script."""
    return ToolCall(id=id or f"call_{name}", name=name, arguments=dict(arguments or {}))


@dataclass
class FakeChatModel(ChatModel):
    turns: list[Script] = field(default_factory=list[Script])
    name: str = "fake"
    #: Every request: (messages, tool names, max_tokens).
    requests: list[tuple[list[ChatMessage], list[str], int]] = field(
        default_factory=list[tuple[list[ChatMessage], list[str], int]]
    )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ModelChunk]:
        self.requests.append((list(messages), [t.name for t in tools], max_tokens))
        script = self.turns.pop(0) if self.turns else Script(text="done")
        if script.delay:
            await anyio.sleep(script.delay)
        if script.error is not None:
            raise ModelError(script.error)
        words = script.text.split(" ") if script.text else []
        for i, word in enumerate(words):
            yield word if i == 0 else f" {word}"
        yield ModelTurn(text=script.text, tool_calls=script.calls, finish_reason="stop")
