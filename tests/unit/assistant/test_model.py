"""OpenAICompatibleModel against a fake HTTP endpoint (no network)."""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx2
import pytest

from stonks.assistant.fake import FakeChatModel, Script, call
from stonks.assistant.model import (
    ChatMessage,
    ModelError,
    ModelTurn,
    OpenAICompatibleModel,
    ToolCall,
    ToolSpec,
    to_openai_messages,
)

KEY = "sk-secret-key-123"


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def _model(handler, key: str | None = KEY) -> OpenAICompatibleModel:
    return OpenAICompatibleModel(
        "http://llm.local/v1", "qwen", api_key=key, transport=httpx2.MockTransport(handler)
    )


def _collect(model, messages=None, tools=()) -> tuple[list[str], ModelTurn]:
    async def run():
        out: list[Any] = []
        async for chunk in model.stream(
            messages or [ChatMessage(role="user", content="hi")],
            list(tools),
            max_tokens=64,
            temperature=0.1,
        ):
            out.append(chunk)
        return out

    chunks = anyio.run(run)
    assert isinstance(chunks[-1], ModelTurn)
    return chunks[:-1], chunks[-1]


def test_streams_text_and_sends_the_request():
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        body = _sse(
            [
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " there"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
            ]
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    tool = ToolSpec("get_portfolio", "your book", {"type": "object", "properties": {}})
    deltas, turn = _collect(_model(handler), tools=[tool])
    assert deltas == ["Hello", " there"]
    assert turn.text == "Hello there"
    assert turn.finish_reason == "stop"
    assert (turn.prompt_tokens, turn.completion_tokens) == (5, 2)
    assert seen["url"] == "http://llm.local/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["stream"] is True
    assert seen["body"]["max_tokens"] == 64
    assert seen["body"]["tools"][0]["function"]["name"] == "get_portfolio"


def test_no_key_sends_no_authorization():
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([]))

    _collect(_model(handler, key=None))
    assert seen["auth"] is None


def test_streamed_tool_calls_are_assembled():
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = _sse(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "function": {"name": "get_bars", "arguments": '{"tic'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": 'ker": "UP.US"}'}}
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 1, "id": "c2", "function": {"name": "whoami"}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ]
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    _, turn = _collect(_model(handler))
    assert turn.tool_calls == (
        ToolCall("c1", "get_bars", {"ticker": "UP.US"}),
        ToolCall("c2", "whoami", {}),
    )
    assert turn.finish_reason == "tool_calls"


def test_bad_arguments_are_kept_raw():
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = _sse(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c",
                                        "function": {"name": "x", "arguments": "{oops"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    _, turn = _collect(_model(handler))
    assert turn.tool_calls[0].arguments == {"_raw": "{oops"}


def test_a_plain_json_answer_is_accepted():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "tool_calls": [
                                {"id": "c9", "function": {"name": "whoami", "arguments": {}}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    deltas, turn = _collect(_model(handler))
    assert deltas == ["ok"]
    assert turn.tool_calls == (ToolCall("c9", "whoami", {}),)


def test_http_errors_are_redacted():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text=f"bad key {KEY}")

    with pytest.raises(ModelError) as info:
        _collect(_model(handler))
    assert "401" in str(info.value)
    assert KEY not in str(info.value)
    assert "***" in str(info.value)


def test_unreachable_endpoint_is_a_model_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    with pytest.raises(ModelError, match="not reachable"):
        _collect(_model(handler))


def test_repr_hides_the_key():
    model = _model(lambda r: httpx2.Response(200))
    assert KEY not in repr(model)


def test_messages_map_to_the_openai_shape():
    out = to_openai_messages(
        [
            ChatMessage(role="assistant", content="", tool_calls=(ToolCall("c", "t", {"a": 1}),)),
            ChatMessage(role="tool", content="{}", tool_call_id="c", name="t"),
        ]
    )
    assert out[0]["tool_calls"][0]["function"] == {"name": "t", "arguments": '{"a": 1}'}
    assert out[1] == {"role": "tool", "content": "{}", "tool_call_id": "c", "name": "t"}


def test_fake_model_replays_its_script():
    model = FakeChatModel([Script(text="one two"), Script(calls=(call("whoami"),))])
    deltas, turn = _collect(model)
    assert deltas == ["one", " two"]
    assert turn.text == "one two"
    _, turn = _collect(model)
    assert turn.tool_calls[0].name == "whoami"
    _, turn = _collect(model)
    assert turn.text == "done"
    assert len(model.requests) == 3


def test_fake_model_error():
    with pytest.raises(ModelError):
        _collect(FakeChatModel([Script(error="boom")]))
