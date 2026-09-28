"""The trusted-host check behind a reverse proxy: Caddy passes the real
Host, so the API accepts exactly the configured names (docs/deploy.md)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.config import load_settings

LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture
def env_app(settings, seeded, monkeypatch, tmp_path):
    """The app built from settings whose [api] host and hosts come from env,
    like the Compose api service."""
    monkeypatch.setenv("STONKS_API_HOST", "0.0.0.0")
    monkeypatch.setenv("STONKS_API_ALLOWED_HOSTS", "stonks.example.com,api")
    from_env = load_settings(config_path=tmp_path / "none.toml").api
    settings.api = settings.api.model_copy(
        update={"host": from_env.host, "allowed_hosts": from_env.allowed_hosts}
    )
    return create_app(settings)


@pytest.mark.parametrize("host", ["stonks.example.com", "api", "api:8000", "localhost:8000"])
def test_an_allowed_host_passes(env_app, host):
    with TestClient(env_app, client=LOOPBACK) as c:
        assert c.get("/api/health/live", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    ["evil.example", "stonks.example.com.evil.example", "0.0.0.0", "sub.stonks.example.com"],
)
def test_any_other_host_is_refused(env_app, host):
    with TestClient(env_app, client=LOOPBACK) as c:
        assert c.get("/api/health/live", headers={"Host": host}).status_code == 400
