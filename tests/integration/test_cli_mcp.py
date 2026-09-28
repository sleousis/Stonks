"""``stonks mcp``: a real stdio MCP session against a subprocess.

The API is deliberately not running (a closed loopback port), so this also
checks the friendly "run stonks serve" error, and that nothing but MCP
protocol reaches stdout (logs must go to stderr or the session breaks).
"""

from __future__ import annotations

import os
import socket
import sys

import pytest
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from stonks.cli import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def closed_port():
    """A loopback port bound but never listening, held for the whole test
    so nothing else can take it: every connection is refused (TT-18,
    BE-69). The socket closes when the test ends."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        yield s.getsockname()[1]


def test_mcp_command_is_registered():
    result = CliRunner().invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "stdio" in result.output


def test_mcp_refuses_token_over_plain_http_to_remote(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        '[mcp]\napi_url = "http://203.0.113.7:8000"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STONKS_API_TOKEN", "tok-" + "x" * 16)
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code != 0
    assert "https" in result.output
    assert "tok-" not in result.output


@pytest.mark.anyio
async def test_stdio_session_reports_unreachable_api(tmp_path, closed_port):
    port = closed_port
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        f'[mcp]\napi_url = "http://127.0.0.1:{port}"\ntimeout_seconds = 5\n'
    )
    token = "secret-" + "y" * 16
    env = {k: v for k, v in os.environ.items() if not k.startswith("STONKS_")}
    env["STONKS_API_TOKEN"] = token
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "stonks.cli", "mcp"], cwd=str(tmp_path), env=env
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        # The API cannot say which tool groups the token may use (roadmap
        # 23.8), so only whoami stays: a limited token never gets more
        # tools by accident. whoami still names the unreachable API.
        assert names == {"whoami"}
        result = await client.call_tool("whoami", {})
    assert result.is_error
    text = result.content[0].text
    assert "stonks serve" in text
    assert f"127.0.0.1:{port}" in text
    assert token not in text
