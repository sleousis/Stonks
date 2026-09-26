"""[brokers] settings section and make_broker factory."""

from __future__ import annotations

import pytest

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.config import BrokersConfig, Settings, load_settings
from stonks.core.types import Portfolio
from stonks.execution.brokers import AlpacaBroker, make_broker
from stonks.execution.brokers.base import BrokerError, LiveTradingRefusedError


@pytest.fixture(autouse=True)
def _no_alpaca_env(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


def test_defaults_are_simulated_and_paper():
    cfg = BrokersConfig()
    assert cfg.kind == "simulated"
    assert cfg.alpaca.paper is True
    assert cfg.alpaca.allow_live is False
    assert cfg.alpaca.api_key is None


def test_brokers_section_reads_from_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        '[brokers]\nkind = "alpaca"\n\n[brokers.alpaca]\npaper = true\nmax_retries = 5\n'
    )
    settings = load_settings(config_path=cfg)
    assert settings.brokers.kind == "alpaca"
    assert settings.brokers.alpaca.max_retries == 5


def test_alpaca_keys_come_from_env_and_are_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "AKTESTKEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "shhh-secret")
    settings = load_settings(config_path=tmp_path / "missing.toml")
    alpaca = settings.brokers.alpaca
    assert alpaca.api_key.get_secret_value() == "AKTESTKEY"
    assert alpaca.secret_key.get_secret_value() == "shhh-secret"
    assert "shhh-secret" not in repr(settings)
    assert "shhh-secret" not in str(settings.model_dump())


def test_env_wins_over_toml_for_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "from-env")
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[brokers.alpaca]\napi_key = "from-toml"\n')
    assert load_settings(config_path=cfg).brokers.alpaca.api_key.get_secret_value() == "from-env"


def test_make_broker_simulated_uses_production_costs():
    settings = Settings(production={"slippage_bps": 5.0, "fee_per_trade": 1.0})
    portfolio = Portfolio(cash=100.0)
    broker = make_broker(settings, portfolio)
    assert isinstance(broker, SimulatedBroker)
    assert broker.fetch_portfolio() is portfolio


def test_make_broker_alpaca_requires_credentials():
    settings = Settings(brokers={"kind": "alpaca"})
    with pytest.raises(BrokerError, match="ALPACA_API_KEY"):
        make_broker(settings, Portfolio(cash=0.0))


def test_make_broker_alpaca_refuses_live_by_default(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    settings = Settings(brokers={"kind": "alpaca", "alpaca": {"paper": False}})
    with pytest.raises(LiveTradingRefusedError):
        make_broker(settings, Portfolio(cash=0.0))


def test_make_broker_alpaca_with_injected_client(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    captured = {}

    class Spy:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("stonks.execution.brokers.alpaca.TradingClient", Spy)
    settings = Settings(brokers={"kind": "alpaca"})
    broker = make_broker(settings, Portfolio(cash=0.0))
    assert isinstance(broker, AlpacaBroker)
    assert captured["paper"] is True


def test_make_broker_kind_override():
    settings = Settings(brokers={"kind": "alpaca"})
    assert isinstance(make_broker(settings, Portfolio(cash=0.0), kind="simulated"), SimulatedBroker)


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        BrokersConfig(kind="ibkr")
