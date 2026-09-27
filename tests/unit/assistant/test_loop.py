"""The agent loop with a scripted model and a fake tool bridge."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest

from stonks.assistant.fake import FakeChatModel, Script, call
from stonks.assistant.loop import DECLINED, MOVED_ON, SKIPPED, ActionConflict, AgentLoop
from stonks.assistant.settings import AssistantConfig
from stonks.assistant.store import ConversationNotFound, ConversationStore
from stonks.assistant.tools import ToolBridge, ToolInfo, ToolOutcome
from stonks.store.state import SqliteState

OWNER = "usr_owner"


class FakeBridge(ToolBridge):
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.delay = delay

    async def tools(self) -> list[ToolInfo]:
        obj = {"type": "object", "properties": {"ticker": {"type": "string"}}}
        guarded = {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "confirm": {"type": "boolean"}},
            "required": ["reason", "confirm"],
        }
        return [
            ToolInfo("get_portfolio", "your book", obj, True, False, False),
            ToolInfo("engage_kill_switch", "stop orders", guarded, False, True, True),
            ToolInfo("delete_price_alert", "delete an alert", obj, False, False, False),
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, dict(arguments)))
        if self.delay:
            await anyio.sleep(self.delay)
        if name == "engage_kill_switch" and not arguments.get("confirm"):
            return ToolOutcome(ok=True, content={"preview": True, "warnings": ["stops orders"]})
        return ToolOutcome(ok=True, content={"tool": name, "cash": 1000})


@pytest.fixture
def store(tmp_path) -> ConversationStore:
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as state:
        state.migrate()
    return ConversationStore(lambda: SqliteState(path))


def _open(store, conv_id: str) -> None:
    """Turn on the categories the fake write tools live in."""
    store.enable_category(conv_id, "risk")
    store.enable_category(conv_id, "alerts")


def _unwrap(content: str) -> dict:
    """The JSON inside the untrusted tool_result wrapper the model sees."""
    assert content.startswith("<tool_result") and 'trust="untrusted"' in content
    return json.loads(content.split("\n", 1)[1].rsplit("\n", 1)[0])


def _run(agen) -> list:
    async def collect():
        return [e async for e in agen]

    return anyio.run(collect)


def _loop(store, turns, bridge=None, **cfg) -> tuple[AgentLoop, FakeChatModel, FakeBridge]:
    model = FakeChatModel(list(turns))
    bridge = bridge or FakeBridge()
    config = AssistantConfig(base_url="http://x", **cfg)
    return AgentLoop(model, bridge, store, config), model, bridge


def kinds(events) -> list[str]:
    return [e.kind for e in events]


def test_plain_answer(store):
    conv = store.create(OWNER)
    loop, model, _ = _loop(store, [Script(text="Hello Alice")])
    events = _run(loop.send(conv.id, "hi"))
    assert kinds(events) == ["text", "text", "done"]
    assert "".join(e.data["delta"] for e in events if e.kind == "text") == "Hello Alice"
    roles = [m.role for m in store.messages(conv.id)]
    assert roles == ["user", "assistant"]
    assert store.get(OWNER, conv.id).title == "hi"
    sent, tools, max_tokens = model.requests[0]
    assert sent[0].role == "system" and "Stonks" in sent[0].content
    assert "get_portfolio" in tools and max_tokens == 1024


def test_read_tool_runs_and_feeds_back(store):
    conv = store.create(OWNER)
    loop, model, bridge = _loop(
        store, [Script(calls=(call("get_portfolio"),)), Script(text="You hold 1000 cash")]
    )
    events = _run(loop.send(conv.id, "my cash?"))
    assert kinds(events)[:2] == ["tool_call", "tool_result"]
    assert events[1].data["ok"] and events[1].data["result"]["cash"] == 1000
    assert bridge.calls == [("get_portfolio", {})]
    tool_msg = model.requests[1][0][-1]
    assert tool_msg.role == "tool" and _unwrap(tool_msg.content)["result"]["cash"] == 1000
    assert events[-1].kind == "done" and events[-1].data["steps"] == 2


def test_write_tool_pauses_then_runs_with_confirm_on_approve(store):
    conv = store.create(OWNER)
    _open(store, conv.id)
    kill = call("engage_kill_switch", {"reason": "panic", "confirm": True})
    loop, model, bridge = _loop(store, [Script(calls=(kill,)), Script(text="Stopped.")])
    events = _run(loop.send(conv.id, "stop everything"))
    assert kinds(events) == ["tool_call", "confirm_required", "done"]
    confirm = events[1].data
    # the model's confirm=true is dropped; the preview comes from confirm=false
    assert confirm["arguments"] == {"reason": "panic"}
    assert confirm["preview"]["preview"] is True
    assert bridge.calls == [("engage_kill_switch", {"reason": "panic", "confirm": False})]
    assert events[-1].data["pending_action_id"] == confirm["action_id"]
    # the model never saw a confirm parameter
    assert "confirm" not in json.dumps(model.requests[0][1])

    action = store.action(conv.id, confirm["action_id"])
    events = _run(loop.decide(conv.id, action, approve=True))
    assert bridge.calls[-1] == ("engage_kill_switch", {"reason": "panic", "confirm": True})
    assert kinds(events) == ["tool_result", "text", "done"]
    assert store.action(conv.id, action.id).status == "done"
    with pytest.raises(ActionConflict):
        _run(loop.decide(conv.id, action, approve=True))


def test_reject_records_the_refusal(store):
    conv = store.create(OWNER)
    _open(store, conv.id)
    loop, model, bridge = _loop(
        store, [Script(calls=(call("delete_price_alert", {"ticker": "UP.US"}),)), Script(text="OK")]
    )
    events = _run(loop.send(conv.id, "note it"))
    action_id = events[1].data["action_id"]
    # no preview for a tool without confirm; nothing ran
    assert events[1].data["preview"] is None and bridge.calls == []
    events = _run(loop.decide(conv.id, store.action(conv.id, action_id), approve=False))
    assert events[0].data["error"] == DECLINED
    assert bridge.calls == []
    assert store.action(conv.id, action_id).status == "rejected"
    assert DECLINED in model.requests[-1][0][-1].content


def test_new_message_rejects_a_waiting_action(store):
    conv = store.create(OWNER)
    _open(store, conv.id)
    loop, model, bridge = _loop(
        store, [Script(calls=(call("delete_price_alert"),)), Script(text="fine")]
    )
    events = _run(loop.send(conv.id, "note"))
    action_id = events[1].data["action_id"]
    _run(loop.send(conv.id, "never mind"))
    assert store.action(conv.id, action_id).status == "rejected"
    history = [m for m in store.messages(conv.id) if m.role == "tool"]
    assert MOVED_ON in history[0].content


def test_calls_after_a_pause_are_skipped(store):
    conv = store.create(OWNER)
    _open(store, conv.id)
    loop, _, bridge = _loop(
        store, [Script(calls=(call("delete_price_alert", id="a"), call("get_portfolio", id="b")))]
    )
    _run(loop.send(conv.id, "x"))
    assert bridge.calls == []
    tools = [m for m in store.messages(conv.id) if m.role == "tool"]
    assert tools[0].tool_call_id == "b" and SKIPPED in tools[0].content


def test_unknown_tool_and_bad_arguments(store):
    conv = store.create(OWNER)
    loop, _, _ = _loop(
        store,
        [
            Script(calls=(call("nope"), call("get_portfolio", {"_raw": "{x"}, id="c2"))),
            Script(text="sorry"),
        ],
    )
    events = _run(loop.send(conv.id, "x"))
    results = [e.data for e in events if e.kind == "tool_result"]
    assert "not available" in results[0]["error"]
    assert "not valid JSON" in results[1]["error"]


def test_max_steps(store):
    conv = store.create(OWNER)
    loop, _, bridge = _loop(
        store, [Script(calls=(call("get_portfolio", id=f"c{i}"),)) for i in range(5)], max_steps=2
    )
    events = _run(loop.send(conv.id, "loop"))
    assert len(bridge.calls) == 2
    error = next(e for e in events if e.kind == "error")
    assert error.data["code"] == "max_steps"
    assert events[-1].kind == "done"


def test_timeout_on_the_model(store):
    conv = store.create(OWNER)
    loop, _, _ = _loop(store, [Script(text="late", delay=1.0)], timeout_seconds=0.05)
    events = _run(loop.send(conv.id, "x"))
    assert [e.data["code"] for e in events if e.kind == "error"] == ["timeout"]


def test_timeout_on_a_tool(store):
    conv = store.create(OWNER)
    loop, _, _ = _loop(
        store,
        [Script(calls=(call("get_portfolio"),)), Script(text="x")],
        bridge=FakeBridge(delay=1.0),
        timeout_seconds=0.1,
    )
    events = _run(loop.send(conv.id, "x"))
    results = [e.data for e in events if e.kind == "tool_result"]
    assert "in time" in results[0]["error"]
    assert [e.data["code"] for e in events if e.kind == "error"] == ["timeout"]


def test_model_error_is_reported(store):
    conv = store.create(OWNER)
    loop, _, _ = _loop(store, [Script(error="endpoint down")])
    events = _run(loop.send(conv.id, "x"))
    assert events[0].kind == "error" and events[0].data["code"] == "model_error"


def test_tool_results_are_capped(store):
    class Big(FakeBridge):
        async def call(self, name, arguments):
            return ToolOutcome(ok=True, content={"blob": "x" * 5000})

    conv = store.create(OWNER)
    loop, model, _ = _loop(
        store,
        [Script(calls=(call("get_portfolio"),)), Script(text="ok")],
        bridge=Big(),
        max_tool_result_chars=300,
    )
    events = _run(loop.send(conv.id, "x"))
    assert "truncated" in next(e for e in events if e.kind == "tool_result").data["result"]
    assert len(model.requests[1][0][-1].content) < 400


def test_history_is_cut_without_orphan_tool_answers(store):
    conv = store.create(OWNER)
    loop, model, _ = _loop(
        store,
        [Script(calls=(call("get_portfolio"),)), Script(text="a"), Script(text="b")],
        max_conversation_messages=2,
    )
    _run(loop.send(conv.id, "one"))
    _run(loop.send(conv.id, "two"))
    sent = model.requests[-1][0]
    assert sent[1].role != "tool"


def test_store_is_per_owner(store):
    conv = store.create(OWNER, "mine")
    with pytest.raises(ConversationNotFound):
        store.get("usr_bob", conv.id)
    with pytest.raises(ConversationNotFound):
        store.delete("usr_bob", conv.id)
    assert store.list("usr_bob", limit=10, offset=0) == ([], 0)
    items, total = store.list(OWNER, limit=10, offset=0)
    assert total == 1 and items[0].title == "mine"
    store.delete(OWNER, conv.id)
    assert store.list(OWNER, limit=10, offset=0)[1] == 0
