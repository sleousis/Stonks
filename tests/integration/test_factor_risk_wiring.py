"""The tick, the backtest and the risk context feed style exposures to the
factor risk model and the style exposure rule (roadmap 22.4), known at the
decision (P12)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import stonks.factors.style as style_mod
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.config import RiskPolicy
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Portfolio
from stonks.portfolio.settings import ConstructionSettings
from stonks.production import hooks as hooks_mod
from stonks.production.hooks import PostTickHook
from stonks.production.risk import build_risk_context
from stonks.production.rules.settings import RuleSettings
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from tests.fixtures.governance import seed_status
from tests.integration.test_covariance_history import (
    DAY,
    UNIVERSE,
    _strategies,
    lakes,  # noqa: F401 - fixture
)
from tests.integration.test_tick_portfolios import People

STYLE = ConstructionSettings(method="erc", params={"estimator": "style"})


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[tuple[str, ...], date]] = []
    real = style_mod.style_exposures

    def spy(lake, tickers, as_of, **kw):
        seen.append((tuple(tickers), as_of))
        return real(lake, tickers, as_of, **kw)

    monkeypatch.setattr(style_mod, "style_exposures", spy)
    return seen


def _tick_weights(tmp_path, lake, construction, monkeypatch):
    folder = tmp_path / "tick"
    folder.mkdir()
    state = SqliteState(folder / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=folder / "artifacts")
    for sid, strategy in _strategies().items():
        registry.register(
            strategy,
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
            strategy_id=sid,
        )
        seed_status(registry, sid, "active")
    people = People(state)
    people.book(
        people.trader("Alice"),
        "Style",
        dict.fromkeys(_strategies(), 1.0),
        construction=construction,
    )
    captured = []

    class Capture(PostTickHook):
        name, stage = "capture_targets", "portfolio"

        def run(self, ctx):
            captured.append(ctx.pipeline.target_book)

    monkeypatch.setitem(hooks_mod._HOOKS, "capture_targets", Capture)
    settings = TickSettings(universe=UNIVERSE, initial_cash=100_000.0)
    try:
        run_tick(state, lake, registry, settings, as_of=DAY, plan=load_tick_plan(state, settings))
    finally:
        state.close()
    [book] = captured
    return {k[0]: v for k, v in book.weights.items()}, book.meta


def _backtest(lake, construction=STYLE, risk=None):
    engine = Backtester(
        strategies=list(_strategies().values()),
        broker=SimulatedBroker(Portfolio(cash=100_000.0)),
        lake=lake,
        config=BacktestConfig(
            start=DAY, end=DAY, universe=UNIVERSE, construction=construction, risk=risk
        ),
    )
    engine.run()
    [book] = engine.target_books.values()
    return {k[0]: v for k, v in book.weights.items()}, book.meta


def test_the_backtest_sizes_on_the_style_model(lakes, calls):  # noqa: F811
    past, planted = lakes
    weights, meta = _backtest(past)
    assert meta["covariance"] == "history"
    assert set(weights) == {"A", "B", "C"}
    assert sum(weights.values()) == pytest.approx(1.0)
    assert calls and all(pd.Timestamp(as_of).date() == DAY for _, as_of in calls)
    assert _backtest(planted) == (weights, meta)  # a planted future changes nothing


def test_tick_and_backtest_agree_on_the_style_model(lakes, tmp_path, monkeypatch, calls):  # noqa: F811
    past, _ = lakes
    construction = {"method": "erc", "estimator": "style"}
    tick, meta = _tick_weights(tmp_path, past, construction, monkeypatch)
    assert meta["covariance"] == "history"
    assert calls
    backtest, _ = _backtest(past)
    assert backtest == pytest.approx(tick, rel=1e-9)


def test_the_rule_gets_exposures_only_when_on(lakes, tmp_path, calls):  # noqa: F811
    past, _ = lakes
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        off = build_risk_context(
            past, state, Portfolio(cash=1.0), {"A.US": 1.0}, DAY, policy=RiskPolicy()
        )
        assert off.factor_exposures is None and not calls
        on_policy = RiskPolicy(
            rules=RuleSettings.model_validate({"style_exposure": {"max_abs_exposure": 0.5}})
        )
        on = build_risk_context(
            past,
            state,
            Portfolio(cash=1.0),
            {"A.US": 1.0},
            DAY,
            policy=on_policy,
            universe=UNIVERSE,
        )
        assert on.factor_exposures is not None
        assert set(on.factor_exposures.index) == set(UNIVERSE)
        assert "momentum" in on.factor_exposures.columns
    finally:
        state.close()


def test_a_backtest_with_the_rule_reads_exposures(lakes, calls):  # noqa: F811
    past, _ = lakes
    policy = RiskPolicy(
        rules=RuleSettings.model_validate({"style_exposure": {"max_abs_exposure": 5.0}})
    )
    _backtest(past, construction=ConstructionSettings(method="erc"), risk=policy)
    assert calls
