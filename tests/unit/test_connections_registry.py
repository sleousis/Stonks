"""Provider registry: discovery, and nothing usable unless enabled."""

from __future__ import annotations

import pytest

from stonks.connections.base import BrokerConnection, ProviderDisabled, ProviderNotConfigured
from stonks.connections.registry import (
    enabled_provider,
    provider_class,
    provider_classes,
    register_provider,
)
from stonks.connections.settings import ConnectionsConfig, SnapTradeConfig


def test_discovers_every_provider_module():
    names = set(provider_classes())
    assert {"alpaca", "snaptrade", "fake", "fake_portal"} <= names
    for name, cls in provider_classes().items():
        assert issubclass(cls, BrokerConnection)
        assert cls.provider == name
        assert cls.auth_flow in ("api_key", "portal")


def test_unknown_provider():
    with pytest.raises(ProviderDisabled, match="unknown"):
        provider_class("nope")


def test_nothing_is_enabled_by_default():
    for name in provider_classes():
        with pytest.raises(ProviderDisabled, match="not enabled"):
            enabled_provider(ConnectionsConfig(), name)


def test_enabled_but_unconfigured_snaptrade_is_refused():
    cfg = ConnectionsConfig(enabled_providers=("snaptrade",))
    with pytest.raises(ProviderNotConfigured):
        enabled_provider(cfg, "snaptrade")
    cfg = ConnectionsConfig(
        enabled_providers=("snaptrade",),
        snaptrade=SnapTradeConfig(client_id="c", consumer_key="k"),
    )
    assert enabled_provider(cfg, "snaptrade").provider == "snaptrade"


def test_enabled_api_key_provider_needs_no_app_config():
    cfg = ConnectionsConfig(enabled_providers=("alpaca",))
    assert enabled_provider(cfg, "alpaca").provider == "alpaca"


def test_register_rejects_bad_or_duplicate_names():
    with pytest.raises(ValueError):
        register_provider("Bad Name")(type("X", (), {}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="twice"):
        register_provider("alpaca")(type("Other", (), {}))  # type: ignore[arg-type]
