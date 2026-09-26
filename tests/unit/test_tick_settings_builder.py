"""One builder turns Settings into what a tick needs (settings, notifier, broker)."""

from __future__ import annotations

import pytest

from stonks.backtest.costs import CostModelSettings
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


def test_dividend_withholding_rate_reaches_the_tick_settings():
    settings = _settings()
    assert build_tick_settings(settings, ["A.US"]).dividend_withholding_rate == 0.0
    settings.production.dividend_withholding_rate = 0.15
    assert build_tick_settings(settings, ["A.US"]).dividend_withholding_rate == 0.15


def test_dividend_withholding_rate_is_bounded():
    with pytest.raises(ValueError):
        Settings(production={"dividend_withholding_rate": 1.5})


def test_configured_backtest_costs_replace_the_legacy_production_costs():
    settings = _settings()
    settings.backtest.costs = CostModelSettings.realistic()
    built = build_tick_settings(settings, ["A.US"])
    assert built.costs == CostModelSettings.realistic()
    # legacy flat costs are dropped, not added on top of the model
    assert (built.slippage_bps, built.fee_per_trade) == (0.0, 0.0)


def test_tick_settings_refuse_a_cost_model_plus_legacy_costs():
    with pytest.raises(ValueError, match="not both"):
        TickSettings(universe=["A.US"], costs=CostModelSettings(), slippage_bps=1.0)


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
