"""MCP per user (roadmap S10): the token names the principal, refusals say
why, and step-up actions point to the web app (hermetic)."""

from __future__ import annotations

import httpx2
import pytest

from stonks.config import Settings
from stonks.mcp.client import ApiClient, ApiError
from stonks.mcp.entry import api_client, mcp_token

TOKEN = "stk_abc_" + "x" * 20
BASE = "http://127.0.0.1:8000"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _answer(status: int, code: str, detail: str) -> ApiClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json={"title": "Forbidden", "status": status, "detail": detail, "code": code}
        )

    return ApiClient(BASE, token=TOKEN, transport=httpx2.MockTransport(handler))


@pytest.mark.anyio
async def test_step_up_refusal_points_to_the_web_app():
    api = _answer(403, "step_up_required", "step_up_required: connection.manage needs ...")
    with pytest.raises(ApiError) as exc:
        await api.post("/api/connections", {})
    message = str(exc.value)
    assert exc.value.status == 403 and exc.value.code == "step_up_required"
    assert "second factor" in message and "web app" in message and BASE in message
    assert TOKEN not in message


@pytest.mark.anyio
async def test_forbidden_names_the_token_role_and_scopes():
    api = _answer(403, "forbidden", "strategy.promote is not allowed for this principal")
    with pytest.raises(ApiError) as exc:
        await api.post("/api/strategies/x/promote", {})
    assert exc.value.code == "forbidden"
    assert "role or scopes" in str(exc.value) and "whoami" in str(exc.value)


@pytest.mark.anyio
async def test_not_found_has_no_hint():
    api = _answer(404, "not_found", "portfolio 'pf_x' not found")
    with pytest.raises(ApiError) as exc:
        await api.get("/api/portfolio", {"portfolio_id": "pf_x"})
    assert exc.value.code == "not_found"
    assert str(exc.value) == "Forbidden (404): portfolio 'pf_x' not found"


@pytest.mark.anyio
async def test_401_names_the_mcp_token():
    api = _answer(401, "not_authenticated", "bad token")
    with pytest.raises(ApiError, match="STONKS_MCP_TOKEN"):
        await api.get("/api/portfolio")


def test_personal_mcp_token_wins_over_the_shared_one(monkeypatch):
    monkeypatch.setenv("STONKS_API_TOKEN", "shared-" + "s" * 16)
    monkeypatch.setenv("STONKS_MCP_TOKEN", TOKEN)
    assert mcp_token(Settings()) == (TOKEN, False)
    assert api_client(Settings()).has_token


def test_shared_token_is_the_fallback_and_is_flagged(monkeypatch):
    monkeypatch.delenv("STONKS_MCP_TOKEN", raising=False)
    monkeypatch.setenv("STONKS_API_TOKEN", "shared-" + "s" * 16)
    assert mcp_token(Settings()) == ("shared-" + "s" * 16, True)
    # A personal token put in the old variable is not flagged.
    monkeypatch.setenv("STONKS_API_TOKEN", TOKEN)
    assert mcp_token(Settings()) == (TOKEN, False)


def test_no_token(monkeypatch):
    monkeypatch.delenv("STONKS_MCP_TOKEN", raising=False)
    monkeypatch.delenv("STONKS_API_TOKEN", raising=False)
    assert mcp_token(Settings()) == (None, False)
