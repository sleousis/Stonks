"""The ``ChatModel`` seam and an OpenAI-compatible client (roadmap 20.4).

Our own types (:class:`ChatMessage`, :class:`ToolSpec`, :class:`ToolCall`,
:class:`ModelTurn`) cross the seam, never a vendor's. A model streams text
deltas (``str``) and ends with one :class:`ModelTurn` that holds the full
text, the tool calls it asked for and the token usage.

:class:`OpenAICompatibleModel` speaks the chat completions API
(``POST {base_url}/chat/completions`` with ``stream: true``) that Ollama,
vLLM and llama.cpp servers all serve. The API key is optional, comes from
the environment only, and is scrubbed from every error.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2

Role = Literal["system", "user", "assistant", "tool"]

#: Characters of an endpoint's error body kept in a :class:`ModelError`.
_MAX_ERROR = 300


class ModelError(RuntimeError):
    """The model endpoint failed (unreachable, HTTP error, bad stream)."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    #: The call a ``tool`` message answers.
    tool_call_id: str | None = None
    #: The tool's name on a ``tool`` message.
    name: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    #: JSON schema of the arguments.
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


#: What :meth:`ChatModel.stream` yields: text deltas, then one turn.
ModelChunk = str | ModelTurn


class ChatModel(ABC):
    """A chat model that can call tools."""

    #: The model name, shown in the status route.
    name: str = "model"

    @abstractmethod
    def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ModelChunk]:
        """Text deltas as they arrive, then exactly one :class:`ModelTurn`."""


def to_openai_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            item["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in m.tool_calls
            ]
        if m.role == "tool":
            item["tool_call_id"] = m.tool_call_id
            if m.name:
                item["name"] = m.name
        out.append(item)
    return out


def to_openai_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def _parse_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    """Tool call arguments: a JSON object string (OpenAI) or an object
    (some local servers). Anything else becomes ``{}`` plus the raw text
    under ``_raw`` so the loop can report the model's mistake."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


class _Accumulator:
    """Builds one turn out of streamed deltas."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None

    def usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            self.prompt_tokens = usage.get("prompt_tokens", self.prompt_tokens)
            self.completion_tokens = usage.get("completion_tokens", self.completion_tokens)

    def call_delta(self, delta: dict[str, Any], position: int) -> None:
        index = delta.get("index", position)
        slot = self.calls.setdefault(int(index), {"id": None, "name": "", "arguments": ""})
        if delta.get("id"):
            slot["id"] = delta["id"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            slot["name"] += fn["name"]
        args = fn.get("arguments")
        if isinstance(args, dict):
            slot["arguments"] = args
        elif isinstance(args, str) and isinstance(slot["arguments"], str):
            slot["arguments"] += args

    def turn(self) -> ModelTurn:
        calls = tuple(
            ToolCall(
                id=slot["id"] or f"call_{i}",
                name=slot["name"],
                arguments=_parse_arguments(slot["arguments"]),
            )
            for i, slot in sorted(self.calls.items())
            if slot["name"]
        )
        return ModelTurn(
            text="".join(self.text),
            tool_calls=calls,
            finish_reason=self.finish_reason,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


class OpenAICompatibleModel(ChatModel):
    """Chat completions over HTTP with streamed deltas and tool calls."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = model
        self._api_key = api_key or None
        self._timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:
        key = "***" if self._api_key else None
        return f"OpenAICompatibleModel(base_url={self.base_url!r}, model={self.name!r}, key={key})"

    def redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ModelChunk]:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": to_openai_messages(messages),
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        headers = {"Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        acc = _Accumulator()
        try:
            async with (
                httpx2.AsyncClient(timeout=self._timeout, transport=self._transport) as http,
                http.stream(
                    "POST", f"{self.base_url}/chat/completions", json=body, headers=headers
                ) as resp,
            ):
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    raise ModelError(
                        self.redact(
                            f"model endpoint answered HTTP {resp.status_code}: {raw[:_MAX_ERROR]}"
                        )
                    )
                content_type = resp.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    # Some servers ignore stream=true: one JSON body.
                    data = json.loads(await resp.aread())
                    for delta in self._apply(acc, data, message_key="message"):
                        yield delta
                else:
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:") :].strip()
                        if payload == "[DONE]":
                            break
                        for delta in self._apply(acc, json.loads(payload), message_key="delta"):
                            yield delta
        except ModelError:
            raise
        except (httpx2.TransportError, httpx2.TimeoutException) as exc:
            raise ModelError(
                self.redact(
                    f"model endpoint not reachable at {self.base_url} ({type(exc).__name__})"
                )
            ) from None
        except ValueError as exc:
            raise ModelError(self.redact(f"model endpoint sent a bad response: {exc}")) from None
        yield acc.turn()

    @staticmethod
    def _apply(acc: _Accumulator, data: Any, *, message_key: str) -> list[str]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        if "error" in data and not data.get("choices"):
            raise ValueError(str(data["error"])[:_MAX_ERROR])
        acc.usage(data.get("usage"))
        out: list[str] = []
        for choice in data.get("choices") or []:
            part = choice.get(message_key) or {}
            text = part.get("content")
            if text:
                acc.text.append(text)
                out.append(text)
            for position, call in enumerate(part.get("tool_calls") or []):
                acc.call_delta(call, position)
            if choice.get("finish_reason"):
                acc.finish_reason = choice["finish_reason"]
        return out
