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


def test_defaults_build_books_from_subscriptions():
    p = Settings().production
    assert p.construction.method == "single_winner"
    assert p.model_books == "shadow"
    # pf_default is subscribed to each active strategy (accounts.default_book)
    assert p.books_from_subscriptions is True


def test_unknown_constructor_fails_at_load_time():
    with pytest.raises(ValueError, match="unknown portfolio constructor"):
        Settings(production={"construction": {"method": "nope"}})


def test_the_plan_comes_from_subscriptions_unless_switched_off(tmp_path, monkeypatch):
    from stonks.production import settings_builder
    from stonks.store.state import SqliteState

    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        off = Settings(production={"books_from_subscriptions": False})
        assert build_tick_runtime(off, ["A.US"]).plan_for(state) is None
        seen = {}

        def fake_load(st, tick_settings, traders=None, dry_run=False):
            seen["args"] = (st, tick_settings)
            seen["traders"] = traders
            return "plan"

        monkeypatch.setattr(settings_builder, "load_tick_plan", fake_load)
        runtime = build_tick_runtime(Settings(), ["A.US"])
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


def test_risk_monitor_and_decay_reach_the_tick_settings():
    from stonks.production.decay import DecaySettings
    from stonks.production.monitor_settings import RiskMonitorSettings

    settings = _settings()
    settings.production.risk_monitor = RiskMonitorSettings(lam=0.9, window=100)
    settings.production.decay = DecaySettings(short_window=40)
    built = build_tick_settings(settings, ["A.US"])
    assert built.risk_monitor == RiskMonitorSettings(lam=0.9, window=100)
    assert built.decay == DecaySettings(short_window=40)


def test_the_risk_monitor_hook_reads_the_tick_settings():
    from stonks.production.decay import DecaySettings
    from stonks.production.hooks.risk_monitor import _settings as hook_settings
    from stonks.production.monitor_settings import RiskMonitorSettings

    tick = TickSettings(
        universe=["A.US"],
        risk_monitor=RiskMonitorSettings(enabled=False),
        decay=DecaySettings(negative_days=5),
    )
    assert hook_settings(tick, "risk_monitor", RiskMonitorSettings).enabled is False
    assert hook_settings(tick, "decay", DecaySettings).negative_days == 5
    assert TickSettings(universe=[]).risk_monitor == RiskMonitorSettings()


def test_the_live_settings_reach_the_tick_settings():
    from stonks.production.live.settings import LiveSettings, SubmitSettings

    settings = _settings()
    settings.production.live = LiveSettings(
        submit_in_window=True, submit=SubmitSettings(window_minutes=30)
    )
    built = build_tick_settings(settings, ["A.US"])
    assert built.live.submit_in_window is True and built.live.submit.window_minutes == 30


def test_the_submit_window_must_end_before_it_starts_is_refused():
    from pydantic import ValidationError

    from stonks.production.live.settings import SubmitSettings

    with pytest.raises(ValidationError):
        SubmitSettings(window_minutes=5, deadline_minutes=5)


def test_the_submit_opener_refuses_a_simulated_portfolio(tmp_path):
    from stonks.production.settings_builder import submit_broker_opener
    from stonks.store.state import SqliteState

    settings = _settings()
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        opener = submit_broker_opener(settings, state)
        with pytest.raises(ValueError, match="does not trade at a broker"):
            opener("pf_default")
        with pytest.raises(ValueError, match="not found"):
            opener("pf_nope")
