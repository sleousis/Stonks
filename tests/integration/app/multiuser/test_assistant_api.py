"""The AI assistant end to end (roadmap 20.4): the REST routes, a scripted
model, and the real MCP tools run in process against the same app as the
signed-in person."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest

from stonks.assistant.fake import FakeChatModel, Script, call
from stonks.assistant.tools import (
    FORBIDDEN_MESSAGE,
    STEP_UP_MESSAGE,
    McpToolBridge,
    friendly_error,
)
from stonks.auth import Principal
from stonks.mcp.client import ApiError

BASE_URL = "http://model.local/v1"


def _events(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if name is not None:
            assert data["kind"] == name
            out.append(data)
    return out


@pytest.fixture
def model(app) -> FakeChatModel:
    services = app.state.services
    services.context.settings.assistant.base_url = BASE_URL
    fake = FakeChatModel()
    services.assistant.model_factory = lambda cfg: fake
    return fake


def _conversation(client, headers) -> str:
    resp = client.post("/api/assistant/conversations", json={}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _say(client, headers, cid: str, text: str) -> list[dict[str, Any]]:
    resp = client.post(
        f"/api/assistant/conversations/{cid}/messages", json={"content": text}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _events(resp.text)


def test_off_without_a_model_endpoint(client, people):
    alice = people["alice"]["headers"]
    status = client.get("/api/assistant/status", headers=alice).json()
    assert status["enabled"] is False and status["model"] is None
    cid = _conversation(client, alice)
    resp = client.post(
        f"/api/assistant/conversations/{cid}/messages", json={"content": "hi"}, headers=alice
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_configured"


def test_read_tool_runs_as_the_signed_in_person(client, people, model):
    alice = people["alice"]
    model.turns = [Script(calls=(call("whoami"),)), Script(text="You are Alice.")]
    cid = _conversation(client, alice["headers"])
    events = _say(client, alice["headers"], cid, "who am I?")
    result = next(e for e in events if e["kind"] == "tool_result")["data"]
    assert result["ok"] is True
    assert result["result"]["user_id"] == alice["id"]
    # the assistant never holds a fresh second factor
    assert result["result"]["via"] == "assistant"
    assert result["result"]["mfa_fresh"] is False
    assert events[-1]["kind"] == "done"
    text = "".join(e["data"]["delta"] for e in events if e["kind"] == "text")
    assert text == "You are Alice."
    # the model saw the MCP tools, without deprecated aliases
    tools = model.requests[0][1]
    assert "get_portfolio" in tools and "health" not in tools
    detail = client.get(f"/api/assistant/conversations/{cid}", headers=alice["headers"]).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert detail["title"] == "who am I?"


def test_write_tool_waits_for_the_person_and_a_viewer_is_refused(client, people, model):
    vic = people["vic"]["headers"]
    model.turns = [
        Script(
            calls=(
                call("engage_kill_switch", {"scope": "user", "reason": "test", "confirm": True}),
            )
        ),
        Script(text="I could not stop trading."),
    ]
    cid = _conversation(client, vic)
    events = _say(client, vic, cid, "stop all my trading")
    confirm = next(e for e in events if e["kind"] == "confirm_required")["data"]
    assert confirm["tool"] == "engage_kill_switch"
    assert confirm["preview"]["preview"] is True
    assert "confirm" not in confirm["arguments"]
    assert events[-1]["data"]["pending_action_id"] == confirm["action_id"]
    # nothing happened yet
    assert client.get("/api/halts", headers=people["ada"]["headers"]).json()["items"] == []

    resp = client.post(
        f"/api/assistant/conversations/{cid}/actions/{confirm['action_id']}",
        json={"approve": True},
        headers=vic,
    )
    assert resp.status_code == 200, resp.text
    events = _events(resp.text)
    result = events[0]["data"]
    assert result["ok"] is False and result["error"] == FORBIDDEN_MESSAGE
    again = client.post(
        f"/api/assistant/conversations/{cid}/actions/{confirm['action_id']}",
        json={"approve": True},
        headers=vic,
    )
    assert again.status_code == 409


def test_trader_approves_the_kill_switch(client, people, model):
    alice = people["alice"]["headers"]
    model.turns = [
        Script(calls=(call("engage_kill_switch", {"scope": "user", "reason": "assistant test"}),)),
        Script(text="Trading is stopped."),
    ]
    cid = _conversation(client, alice)
    events = _say(client, alice, cid, "stop trading")
    action_id = events[-1]["data"]["pending_action_id"]
    resp = client.post(
        f"/api/assistant/conversations/{cid}/actions/{action_id}",
        json={"approve": True},
        headers=alice,
    )
    result = _events(resp.text)[0]["data"]
    assert result["ok"] is True and result["result"]["applied"] is True
    halts = client.get("/api/halts", headers=alice).json()["items"]
    assert [h["kind"] for h in halts] == ["kill"]


def test_step_up_routes_are_refused_with_a_web_app_pointer(app, people):
    alice = people["alice"]
    principal = Principal.create(
        user_id=alice["id"],
        kind="human",
        role=alice["role"],
        scopes=["read", "trade", "lab"],
        mfa_fresh=True,
        via="session",
    )
    bridge = McpToolBridge(app, principal)

    async def attempt() -> ApiError:
        try:
            await bridge._api.post("/api/halts/1/resume", {"confirmation": "x", "reason": "y"})
        except ApiError as exc:
            return exc
        finally:
            await bridge.aclose()
        raise AssertionError("resume should need step-up")

    error = anyio.run(attempt)
    assert error.code == "step_up_required"
    assert friendly_error(str(error)) == STEP_UP_MESSAGE


def test_conversations_are_private(client, people, model):
    alice, bob = people["alice"]["headers"], people["bob"]["headers"]
    cid = _conversation(client, alice)
    assert client.get(f"/api/assistant/conversations/{cid}", headers=bob).status_code == 404
    resp = client.post(
        f"/api/assistant/conversations/{cid}/messages", json={"content": "x"}, headers=bob
    )
    assert resp.status_code == 404
    assert client.delete(f"/api/assistant/conversations/{cid}", headers=bob).status_code == 404
    assert client.get("/api/assistant/conversations", headers=bob).json()["total"] == 0
    listed = client.get("/api/assistant/conversations", headers=alice).json()
    assert [c["id"] for c in listed["items"]] == [cid]
    assert client.delete(f"/api/assistant/conversations/{cid}", headers=alice).status_code == 204
    assert client.get(f"/api/assistant/conversations/{cid}", headers=alice).status_code == 404


def test_unknown_action_is_404(client, people, model):
    alice = people["alice"]["headers"]
    cid = _conversation(client, alice)
    resp = client.post(
        f"/api/assistant/conversations/{cid}/actions/act_missing",
        json={"approve": False},
        headers=alice,
    )
    assert resp.status_code == 404
