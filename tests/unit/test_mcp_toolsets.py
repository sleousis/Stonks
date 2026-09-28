"""MCP toolset allowlists per API token (roadmap 23.8)."""

from __future__ import annotations

import anyio
import pytest

from stonks.accounts import Role
from stonks.auth import ApiScope
from stonks.auth.principal import Principal
from stonks.mcp import entry
from stonks.mcp.client import ApiClient
from stonks.mcp.server import build_server
from stonks.mcp.toolsets import ALWAYS, TOOLSET_NAMES, allowed, toolset_about, unknown_toolsets


def _names(toolsets):
    async def run():
        api = ApiClient("http://127.0.0.1:1", token=None)
        try:
            return {t.name for t in await build_server(api, toolsets=toolsets).list_tools()}
        finally:
            await api.aclose()

    return anyio.run(run)


def test_groups_are_the_tool_modules():
    assert {"reads", "jobs", "orders", "risk", "decisions"} <= set(TOOLSET_NAMES)
    about = dict(toolset_about())
    assert about["decisions"].startswith("Why did or didn't we trade")
    assert unknown_toolsets(["risk", "nope"]) == ["nope"]


def test_a_limited_token_sees_only_its_groups_and_whoami():
    everything = _names(None)
    limited = _names({"risk", "decisions"})
    assert "list_trade_decisions" in limited and "get_live_risk" in limited
    assert "place_order" in everything and "place_order" not in limited
    assert "run_tick" not in limited and "get_portfolio" not in limited
    assert limited >= ALWAYS
    assert _names(frozenset()) == set(ALWAYS)


def test_allowed_rules():
    assert allowed("anything", "orders", None)
    assert allowed("whoami", "reads", set())
    assert not allowed("place_order", "orders", {"risk"})
    assert not allowed("mystery", None, {"risk"})


def test_principal_keeps_its_toolsets():
    p = Principal.create(
        user_id="u",
        kind="human",
        role=Role.TRADER,
        scopes=[ApiScope.READ],
        mfa_fresh=False,
        via="token",
        toolsets=["risk"],
    )
    assert p.toolsets == frozenset({"risk"})
    from stonks.assistant.tools import assistant_principal

    assert assistant_principal(p).toolsets == frozenset({"risk"})


class _Api:
    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self.answer, self.error, self.closed = answer, error, False

    async def get(self, path):
        assert path == "/api/auth/me"
        if self.error:
            raise self.error
        return self.answer

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    ("answer", "expected"),
    [({"toolsets": None}, None), ({"toolsets": ["risk"]}, frozenset({"risk"})), ({}, None)],
)
def test_the_server_reads_its_toolsets_from_me(answer, expected):
    api = _Api(answer)
    assert entry.resolve_toolsets(api) == expected  # type: ignore[arg-type]
    assert api.closed


def test_an_unknown_answer_fails_closed():
    api = _Api(error=RuntimeError("down"))
    assert entry.resolve_toolsets(api) == frozenset()  # type: ignore[arg-type]
