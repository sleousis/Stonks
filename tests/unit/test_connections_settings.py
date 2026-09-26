"""[connections] settings: nothing enabled by default, secrets env-only."""

from __future__ import annotations

import pytest

from stonks.connections.settings import ConnectionsConfig


def test_defaults_enable_nothing(tmp_path):
    cfg = ConnectionsConfig.load(tmp_path / "missing.toml", environ={})
    assert cfg.enabled_providers == ()
    assert not cfg.is_enabled("snaptrade")
    assert not cfg.snaptrade.configured


def test_toml_and_env(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[connections]\nenabled_providers = ["SnapTrade"]\n'
        '[connections.snaptrade]\nclient_id = "cid"\n'
    )
    cfg = ConnectionsConfig.load(path, environ={"STONKS_SNAPTRADE_CONSUMER_KEY": "ck-secret"})
    assert cfg.enabled_providers == ("snaptrade",)
    assert cfg.snaptrade.client_id == "cid"
    assert cfg.snaptrade.configured
    assert "ck-secret" not in repr(cfg)


def test_env_overrides_enabled_providers(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[connections]\nenabled_providers = ["snaptrade"]\n')
    env = {"STONKS_CONNECTIONS_ENABLED_PROVIDERS": "alpaca, fake,alpaca"}
    assert ConnectionsConfig.load(path, environ=env).enabled_providers == ("alpaca", "fake")
    env = {"STONKS_CONNECTIONS_ENABLED_PROVIDERS": ""}
    assert ConnectionsConfig.load(path, environ=env).enabled_providers == ()


def test_consumer_key_in_toml_is_refused_without_echo(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[connections.snaptrade]\nconsumer_key = "leaky-value"\n')
    with pytest.raises(ValueError) as info:
        ConnectionsConfig.load(path, environ={})
    assert "leaky-value" not in str(info.value)
    assert "STONKS_SNAPTRADE_CONSUMER_KEY" in str(info.value)


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[connections]\nenabled = true\n")
    with pytest.raises(ValueError):
        ConnectionsConfig.load(path, environ={})
