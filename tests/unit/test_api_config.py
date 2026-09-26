"""Unit tests for the [api] settings section."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks.config import ApiConfig, load_settings


def test_api_defaults_bind_loopback_and_angular_origin(monkeypatch):
    monkeypatch.delenv("STONKS_API_TOKEN", raising=False)
    cfg = ApiConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.ui_origin == "http://localhost:4200"
    assert cfg.open_reads_on_loopback is True
    assert cfg.max_concurrent_jobs >= 1
    assert cfg.token is None
    assert cfg.ui_dist == Path("web/dist")


def test_api_token_comes_from_env(monkeypatch):
    monkeypatch.setenv("STONKS_API_TOKEN", "s3cret")
    cfg = ApiConfig()
    assert cfg.token is not None
    assert cfg.token.get_secret_value() == "s3cret"
    # SecretStr keeps the value out of reprs / logs.
    assert "s3cret" not in repr(cfg)


def test_api_token_in_toml_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("STONKS_API_TOKEN", raising=False)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[api]\ntoken = "checked-in"\n')
    with pytest.raises(ValidationError, match="STONKS_API_TOKEN"):
        load_settings(config_path=cfg)


def test_api_section_reads_from_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("STONKS_API_TOKEN", raising=False)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        '[api]\nport = 9001\nui_origin = "http://localhost:4300"\n'
        "open_reads_on_loopback = false\nmax_concurrent_jobs = 3\n"
    )
    settings = load_settings(config_path=cfg)
    assert settings.api.port == 9001
    assert settings.api.ui_origin == "http://localhost:4300"
    assert settings.api.open_reads_on_loopback is False
    assert settings.api.max_concurrent_jobs == 3


def test_default_toml_has_api_section():
    settings = load_settings(config_path=Path("config/default.toml"))
    assert settings.api.host == "127.0.0.1"
