"""One builder turns Settings into what a tick needs (settings, notifier, broker)."""

from __future__ import annotations

from stonks.config import Settings
from stonks.notify import CompositeNotifier
from stonks.production.settings_builder import build_tick_runtime, build_tick_settings
from stonks.production.tick import TickSettings


def _settings() -> Settings:
    return Settings(
        production={
            "universe": ["CFG.US"],
            "threshold": 0.02,
            "initial_cash": 5_000.0,
            "slippage_bps": 3.0,
            "fee_per_trade": 1.5,
            "max_price_staleness_days": 3,
            "shadow_enabled": False,
            "risk": {"max_open_positions": 2, "max_weight_per_ticker": 0.4},
        },
        notify={"backends": ["log"], "min_level": "info"},
    )


def test_build_tick_settings_maps_every_production_field():
    settings = _settings()
    built = build_tick_settings(settings, ["A.US", "B.US"])
    assert built == TickSettings(
        universe=["A.US", "B.US"],
        threshold=0.02,
        initial_cash=5_000.0,
        slippage_bps=3.0,
        fee_per_trade=1.5,
        max_price_staleness_days=3,
        risk=settings.production.risk,
        shadow_enabled=False,
        broker_kind="simulated",
    )


def test_build_tick_runtime_includes_a_notifier_from_notify_config():
    runtime = build_tick_runtime(_settings(), ["A.US"])
    assert isinstance(runtime.notifier, CompositeNotifier)
    assert runtime.notifier.min_level == "info"
    assert runtime.settings.universe == ["A.US"]


def test_default_runtime_is_simulated_and_needs_no_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    runtime = build_tick_runtime(Settings(), ["A.US"])
    assert runtime.settings.broker_kind == "simulated"
