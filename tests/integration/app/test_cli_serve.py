"""``stonks serve`` wires uvicorn to the app factory."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[tuple, dict]] = []

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_serve_defaults_to_loopback(captured):
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    (args, kwargs) = captured[0]
    assert args[0] == "stonks.api.server:app_factory"
    assert kwargs["factory"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8000
    assert kwargs["reload"] is False


def test_serve_options_override(captured):
    result = CliRunner().invoke(app, ["serve", "--host", "::1", "--port", "9123", "--reload"])
    assert result.exit_code == 0, result.output
    _, kwargs = captured[0]
    assert kwargs["host"] == "::1"
    assert kwargs["port"] == 9123
    assert kwargs["reload"] is True


def test_app_factory_builds_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path / "data"))
    from stonks.api.server import app_factory

    fastapi_app = app_factory()
    assert fastapi_app.title == "Stonks API"


def test_app_factory_silences_uvicorn_access_log(monkeypatch, tmp_path):
    """uvicorn's access log prints query strings, which can carry a stream
    token; the app's own request log records the path only."""
    import logging

    from stonks.api import server

    monkeypatch.chdir(tmp_path)
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "disabled", False)
    server.app_factory()
    assert access.disabled is True
