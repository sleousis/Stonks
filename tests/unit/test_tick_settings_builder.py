"""One builder turns Settings into what a tick needs (settings, notifier, broker)."""

from __future__ import annotations

import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.config import Settings
from stonks.lab.parallel import default_max_workers
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
        scoring_workers=default_max_workers(),  # scoring_workers = 0: every core
    )


def test_scoring_workers_reach_the_tick_settings():
    settings = _settings()
    settings.production.scoring_workers = 3
    settings.production.parallel_min_estimates = 50
    built = build_tick_settings(settings, ["A.US"])
    assert (built.scoring_workers, built.parallel_min_estimates) == (3, 50)


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


# ---- W2.1: construction, model books, books from subscriptions ----------------


def test_construction_and_model_books_reach_the_tick_settings():
    settings = Settings(
        production={
            "construction": {"method": "equal_weight_top_n", "buffer_fraction": 0.2},
            "model_books": "all",
        }
    )
    built = build_tick_settings(settings, ["A.US"])
    assert built.construction.method == "equal_weight_top_n"
    assert built.construction.buffer_fraction == 0.2
    assert built.model_books == "all"


def test_defaults_keep_todays_single_book():
    p = Settings().production
    assert p.construction.method == "single_winner"
    assert p.model_books == "shadow"
    assert p.books_from_subscriptions is False


def test_unknown_constructor_fails_at_load_time():
    with pytest.raises(ValueError, match="unknown portfolio constructor"):
        Settings(production={"construction": {"method": "nope"}})


def test_the_plan_comes_from_subscriptions_only_when_switched_on(tmp_path, monkeypatch):
    from stonks.production import settings_builder
    from stonks.store.state import SqliteState

    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        assert build_tick_runtime(Settings(), ["A.US"]).plan_for(state) is None
        seen = {}

        def fake_load(st, tick_settings, traders=None):
            seen["args"] = (st, tick_settings)
            seen["traders"] = traders
            return "plan"

        monkeypatch.setattr(settings_builder, "load_tick_plan", fake_load)
        runtime = build_tick_runtime(
            Settings(production={"books_from_subscriptions": True}), ["A.US"]
        )
        assert runtime.plan_for(state) == "plan"
        assert seen["args"] == (state, runtime.settings)
        assert callable(seen["traders"])  # auto books trade through their connection
    finally:
        state.close()


def test_the_quit_rule_reaches_the_tick_settings():
    settings = _settings()
    settings.production.quit_rule = settings.production.quit_rule.model_copy(
        update={"auto_demote": True}
    )
    assert build_tick_settings(settings, ["A.US"]).quit_rule.auto_demote is True
