"""A keyword-driven :class:`ChatModel` for the e2e stack, so the assistant
journeys run without a model server.

It answers the last message:

- a question with "stop trading" turns on the risk tools and asks for the
  kill switch (a write, so the chat asks the person first);
- a question with "portfolio" reads the portfolio;
- a tool result gets a one-line answer (declined, refused or read);
- anything else gets a greeting.

Text streams word by word, like a real endpoint.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Sequence

from stonks.assistant.model import ChatMessage, ChatModel, ModelChunk, ModelTurn, ToolCall, ToolSpec

GREETING = "Hello. Ask me about your portfolio."
READ_ANSWER = "I read your portfolio: it holds only cash for now."
DECLINED_ANSWER = "Understood. I left trading as it is."
REFUSED_ANSWER = "I could not do that here, so nothing changed."


class KeywordChatModel(ChatModel):
    name = "e2e-keyword"

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def _call(self, name: str, arguments: dict[str, object]) -> ToolCall:
        return ToolCall(id=f"call_{name}_{next(self._ids)}", name=name, arguments=dict(arguments))

    def _reply(self, messages: Sequence[ChatMessage]) -> ModelTurn:
        last = messages[-1] if messages else None
        if last is None:
            return ModelTurn(text=GREETING)
        if last.role == "tool":
            content = last.content.lower()
            if "declined" in content:
                return ModelTurn(text=DECLINED_ANSWER)
            if '"ok": false' in content or '"ok":false' in content:
                return ModelTurn(text=REFUSED_ANSWER)
            return ModelTurn(text=READ_ANSWER)
        text = last.content.lower()
        if "stop trading" in text:
            return ModelTurn(
                text="I will ask you before I stop anything.",
                tool_calls=(
                    self._call("enable_tool_category", {"category": "risk"}),
                    self._call(
                        "engage_kill_switch", {"scope": "user", "reason": "Asked in the chat"}
                    ),
                ),
            )
        if "portfolio" in text:
            return ModelTurn(text="Let me look.", tool_calls=(self._call("get_portfolio", {}),))
        return ModelTurn(text=GREETING)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ModelChunk]:
        turn = self._reply(messages)
        for i, word in enumerate(turn.text.split(" ")):
            yield word if i == 0 else f" {word}"
        yield ModelTurn(text=turn.text, tool_calls=turn.tool_calls, finish_reason="stop")
