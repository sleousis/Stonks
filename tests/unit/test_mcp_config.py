"""The ``[mcp]`` settings section."""

from __future__ import annotations

from pathlib import Path

import pytest

from stonks.config import McpConfig, Settings, load_settings


def test_mcp_defaults_point_at_local_api():
    cfg = Settings().mcp
    assert cfg.api_url == "http://127.0.0.1:8000"
    assert cfg.timeout_seconds == 30.0
    assert cfg.max_wait_seconds == 600.0


def test_mcp_section_reads_from_toml(tmp_path):
    path = tmp_path / "cfg.toml"
    path.write_text('[mcp]\napi_url = "http://localhost:9000"\ntimeout_seconds = 5\n')
    assert load_settings(config_path=path).mcp.api_url == "http://localhost:9000"


def test_mcp_rejects_unknown_keys_and_token():
    with pytest.raises(ValueError):
        McpConfig(token="x")  # the token is env-only (STONKS_API_TOKEN)


def test_default_toml_has_mcp_section():
    settings = load_settings(config_path=Path("config/default.toml"))
    assert settings.mcp.api_url == "http://127.0.0.1:8000"
