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


def test_ibkr_gateways_come_from_brokers_ibkr(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[brokers.ibkr.gateways.paper]\nhost = "gw"\nport = 4004\nmode = "paper"\n'
        '[brokers.ibkr.flex]\nquery_id = "123"\n'
    )
    cfg = ConnectionsConfig.load(path, environ={})
    assert cfg.ibkr.gateways["paper"].port == 4004
    assert cfg.ibkr.flex.query_id == "123"
    assert ConnectionsConfig.load(tmp_path / "none.toml", environ={}).ibkr.gateways == {}
    path.write_text("[connections.ibkr]\nallow_live = true\n")
    with pytest.raises(ValueError, match=r"\[brokers.ibkr\]"):
        ConnectionsConfig.load(path, environ={})


def test_etoro_is_off_and_trades_nothing_by_default(tmp_path):
    cfg = ConnectionsConfig.load(tmp_path / "missing.toml", environ={})
    assert not cfg.is_enabled("etoro")
    assert cfg.etoro.trading is False
    assert cfg.etoro.allow_real_money is False
    assert cfg.etoro.base_url == "https://public-api.etoro.com"
    # our own budgets stay under eToro's (60 reads, 20 orders a minute)
    assert cfg.etoro.reads_per_minute <= 60
    assert cfg.etoro.orders_per_minute <= 20


def test_etoro_settings_come_from_toml(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[connections]\nenabled_providers = ["etoro"]\n'
        "[connections.etoro]\ntrading = true\norders_per_minute = 5\n"
        '[connections.etoro.instrument_overrides]\n"AAPL.US" = 1001\n'
    )
    cfg = ConnectionsConfig.load(path, environ={})
    assert cfg.is_enabled("etoro")
    assert cfg.etoro.trading is True
    assert cfg.etoro.orders_per_minute == 5
    assert cfg.etoro.instrument_overrides == {"AAPL.US": 1001}


@pytest.mark.parametrize("field", ["api_key", "user_key", "x_api_key"])
def test_etoro_keys_in_toml_are_refused_without_echo(tmp_path, field):
    path = tmp_path / "c.toml"
    path.write_text(f'[connections.etoro]\n{field} = "leaky-value"\n')
    with pytest.raises(ValueError) as info:
        ConnectionsConfig.load(path, environ={})
    assert "leaky-value" not in str(info.value)


def test_etoro_budgets_cannot_exceed_etoros_limits(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[connections.etoro]\norders_per_minute = 21\n")
    with pytest.raises(ValueError):
        ConnectionsConfig.load(path, environ={})
    path.write_text("[connections.etoro]\nreads_per_minute = 61\n")
    with pytest.raises(ValueError):
        ConnectionsConfig.load(path, environ={})
