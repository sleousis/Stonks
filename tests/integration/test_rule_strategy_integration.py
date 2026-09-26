"""RuleStrategy end to end: backtest on a real lake, registry round-trip,
lab run of a bound spec, production ranking."""

from __future__ import annotations

from datetime import date

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.tuning.random import RandomTuner
from stonks.production.ranker import Ranker
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.rule_based import RULE_STRATEGY_CLASS_PATH, RuleStrategy
from stonks.strategies.rules import TEMPLATES
from tests.fixtures.governance import seed_status

UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]

# Uptrend filter: 5-bar SMA above 20-bar SMA → only UP.US qualifies.
TREND = {
    "version": 1,
    "name": "trend",
    "indicators": [
        {"id": "fast", "kind": "sma", "period": 5},
        {"id": "slow", "kind": "sma", "period": 20},
        {"id": "roc", "kind": "roc", "period": 10},
    ],
    "entry": {
        "type": "compare",
        "left": {"type": "indicator", "id": "fast"},
        "op": ">",
        "right": {"type": "indicator", "id": "slow"},
    },
    "rank": {"by": "roc"},
    "sizing": {"max_positions": 2, "allocation": 1.0},
}


def test_backtest_on_real_lake(lake_trending):
    broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0))
    report = Backtester(
        strategies=[RuleStrategy({"spec": TREND})],
        broker=broker,
        lake=lake_trending,
        config=BacktestConfig(start=date(2025, 11, 1), end=date(2026, 4, 1), universe=UNIVERSE),
    ).run()
    assert report.final_return > 0.2
    held = broker.fetch_portfolio().positions
    assert set(held) == {"UP.US"}


def test_templates_backtest_without_errors(lake_trending):
    for template in TEMPLATES.values():
        report = Backtester(
            strategies=[RuleStrategy({"spec": template.spec})],
            broker=SimulatedBroker(portfolio=Portfolio(cash=10_000.0)),
            lake=lake_trending,
            config=BacktestConfig(start=date(2025, 12, 1), end=date(2026, 4, 1), universe=UNIVERSE),
        ).run()
        assert report.equity_curve, template.id


def test_registry_round_trip(tmp_path, lake_trending):
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        original = RuleStrategy({"spec": TREND})
        sid = registry.register(original, reports=[])
        (handle,) = registry.list_all()
        assert handle.class_path == RULE_STRATEGY_CLASS_PATH
        assert handle.params["spec"]["name"] == "trend"
        loaded = registry.load(sid)
        assert isinstance(loaded, RuleStrategy)
        assert loaded.spec == original.spec
        as_of = date(2026, 3, 2)
        assert loaded.estimate_return("UP.US", as_of, lake_trending) == original.estimate_return(
            "UP.US", as_of, lake_trending
        )


def test_lab_run_of_bound_spec_registers_as_rule_strategy(tmp_path, lake_trending):
    ds = LabDataset(
        lake=lake_trending,
        universe=UNIVERSE,
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
    )
    runner = LabRunner(
        tuner=RandomTuner(0),
        objective=SharpeObjective(),
        suite=SurvivalSuite(tests=[OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99)]),
        budget=1,
    )
    result = runner.run(RuleStrategy.bind(TREND), ds)
    assert result.best_params["spec"]["name"] == "trend"
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        sid = registry.register(result.strategy, result.survival_reports)
        assert registry.list_all()[0].class_path == RULE_STRATEGY_CLASS_PATH
        assert registry.load(sid).spec.name == "trend"


def test_ranker_filters_by_spec_asset_classes(tmp_path, lake_trending):
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        crypto_only = dict(TREND, universe={"asset_classes": ["crypto"]})
        eq = registry.register(RuleStrategy({"spec": TREND}), reports=[])
        cr = registry.register(RuleStrategy({"spec": crypto_only}), reports=[])
        seed_status(registry, eq, "active")
        seed_status(registry, cr, "active")
        ranked = Ranker(registry=registry, lake=lake_trending, universe=UNIVERSE).rank(
            as_of=date(2026, 3, 2)
        )
        assert {(sid, t) for _, sid, t in ranked} == {(eq, "UP.US")}
