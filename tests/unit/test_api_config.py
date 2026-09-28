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
    # Reads need a credential unless the dev profile turns this on.
    assert cfg.open_reads_on_loopback is False
    assert cfg.trusted_proxies == ["127.0.0.1"]
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


def test_default_toml_has_api_section(monkeypatch):
    monkeypatch.delenv("STONKS_PROFILE", raising=False)
    settings = load_settings(config_path=Path("config/default.toml"))
    assert settings.api.host == "127.0.0.1"
    assert settings.api.open_reads_on_loopback is False


def test_dev_profile_opens_loopback_reads(monkeypatch):
    monkeypatch.setenv("STONKS_PROFILE", "dev")
    settings = load_settings(config_path=Path("config/default.toml"))
    assert settings.api.open_reads_on_loopback is True


def test_trusted_proxies_from_toml_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("STONKS_API_TRUSTED_PROXIES", raising=False)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[api]\ntrusted_proxies = ["10.0.0.2"]\n')
    assert load_settings(config_path=cfg).api.trusted_proxies == ["10.0.0.2"]
    monkeypatch.setenv("STONKS_API_TRUSTED_PROXIES", "172.31.250.0/24, 127.0.0.1")
    assert load_settings(config_path=cfg).api.trusted_proxies == ["172.31.250.0/24", "127.0.0.1"]


def test_auth_section_from_toml_with_env_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[auth]\nsession_idle_hours = 4.0\nmax_failures = 7\n")
    monkeypatch.delenv("STONKS_AUTH_MAX_FAILURES", raising=False)
    monkeypatch.setenv("STONKS_AUTH_COOKIE_SECURE", "false")
    settings = load_settings(config_path=cfg)
    assert settings.auth.session_idle_hours == 4.0
    assert settings.auth.max_failures == 7
    assert settings.auth.cookie_secure is False
    monkeypatch.setenv("STONKS_AUTH_MAX_FAILURES", "3")
    assert load_settings(config_path=cfg).auth.max_failures == 3


def test_trusted_proxies_must_be_addresses_or_networks():
    with pytest.raises(ValidationError):
        ApiConfig(trusted_proxies=["caddy"])


def test_api_host_and_allowed_hosts_from_env(tmp_path, monkeypatch):
    """Compose sets both: Caddy passes the real Host (the domain) and the
    scheduler calls http://api:8000."""
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[api]\nallowed_hosts = ["toml.example"]\n')
    monkeypatch.delenv("STONKS_API_HOST", raising=False)
    monkeypatch.delenv("STONKS_API_ALLOWED_HOSTS", raising=False)
    settings = load_settings(config_path=cfg)
    assert settings.api.host == "127.0.0.1"
    assert settings.api.allowed_hosts == ["toml.example"]
    monkeypatch.setenv("STONKS_API_HOST", "0.0.0.0")
    monkeypatch.setenv("STONKS_API_ALLOWED_HOSTS", " Stonks.Example.com , api,, ")
    settings = load_settings(config_path=cfg)
    assert settings.api.host == "0.0.0.0"
    assert settings.api.allowed_hosts == ["stonks.example.com", "api"]


def test_blank_env_hosts_keep_the_file_values(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[api]\nhost = "10.0.0.5"\nallowed_hosts = ["toml.example"]\n')
    monkeypatch.setenv("STONKS_API_HOST", "  ")
    monkeypatch.setenv("STONKS_API_ALLOWED_HOSTS", "")
    settings = load_settings(config_path=cfg)
    assert settings.api.host == "10.0.0.5"
    assert settings.api.allowed_hosts == ["toml.example"]


@pytest.mark.parametrize(
    "bad", ["*", "evil*.example.com", "*.*.example.com", "https://x.example", "x.example/path"]
)
def test_allowed_hosts_refuse_catch_alls_and_urls(bad):
    with pytest.raises(ValidationError, match="allowed_hosts"):
        ApiConfig(allowed_hosts=[bad])


def test_allowed_hosts_accept_names_and_subdomain_wildcards():
    cfg = ApiConfig(allowed_hosts=["stonks.example.com", "*.ts.net", "api", "10.1.2.3"])
    assert cfg.allowed_hosts == ["stonks.example.com", "*.ts.net", "api", "10.1.2.3"]
