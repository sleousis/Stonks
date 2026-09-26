"""The backtest runs the production construction pipeline when
``BacktestConfig.construction`` is set (BL-12, W2.1)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.config import RiskPolicy
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Portfolio
from stonks.production import hooks as hooks_mod
from stonks.production.hooks import PostTickHook
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status
from tests.integration.test_tick_portfolios import People

UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]


def _run(lake, strategies, **config):
    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    engine = Backtester(
        strategies=strategies,
        broker=broker,
        lake=lake,
        config=BacktestConfig(
            start=config.pop("start", date(2026, 1, 5)),
            end=config.pop("end", date(2026, 3, 20)),
            universe=UNIVERSE,
            **config,
        ),
    )
    return engine, engine.run(), broker


def test_single_winner_through_the_pipeline_matches_the_per_strategy_engine(lake_trending):
    """With one strategy the winner's own decide makes the orders, so the
    equity curve is the legacy engine's."""

    def strategy():
        return Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.5})

    _, legacy, _ = _run(lake_trending, [strategy()])
    _, piped, _ = _run(lake_trending, [strategy()], construction="single_winner")
    assert piped.equity_curve == legacy.equity_curve
    assert len(legacy.equity_curve) > 20


def test_two_strategies_both_receive_capital(lake_trending):
    engine, _, broker = _run(
        lake_trending,
        [
            BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
            BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}),
        ],
        construction="equal_weight_top_n",
    )
    keys = {f.order_client_id.partition(":")[0] for f in broker.fills}
    assert keys == {"0", "1"}  # the trade ledger's per-strategy key
    held = broker.fetch_portfolio().positions
    assert set(held) == {"UP.US", "FLAT.US"}
    last = engine.target_books[max(engine.target_books)]
    assert last.weights == pytest.approx({"UP.US": 0.5, "FLAT.US": 0.5})
    assert last.attribution == {"UP.US": {"0": 1.0}, "FLAT.US": {"1": 1.0}}


def test_strategy_weights_and_the_risk_policy_apply(lake_trending):
    engine, _, broker = _run(
        lake_trending,
        [
            BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
            BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}),
        ],
        construction="equal_weight_top_n",
        strategy_weights=[1.0, 0.0],
        risk=RiskPolicy(max_weight_per_ticker=0.3),
    )
    book = broker.fetch_portfolio()
    assert set(book.positions) == {"UP.US"}
    price = lake_trending.sql(
        "SELECT close FROM prices WHERE ticker = 'UP.US' AND date <= '2026-03-20'"
        " ORDER BY date DESC LIMIT 1"
    )["close"].iloc[0]
    value = book.positions["UP.US"] * price
    # Buys stop at 30% of equity; the rise since then only drifts it up.
    assert 0.29 < value / (book.cash + value) < 0.4
    _, _, uncapped = _run(
        lake_trending,
        [BuyAndHold({"ticker": "UP.US", "allocation": 1.0})],
        construction="equal_weight_top_n",
    )
    assert uncapped.fetch_portfolio().cash < 0.15 * 10_000.0  # the no-trade band


def test_decisions_never_see_history_after_their_bar(lake_trending):
    engine, _, _ = _run(
        lake_trending,
        [BuyAndHold({"ticker": "UP.US", "allocation": 1.0})],
        construction="inverse_vol",
        start=date(2026, 3, 2),
        end=date(2026, 3, 6),
    )
    for as_of in engine.target_books:
        seen = engine._history_until(as_of)
        assert all(frame.index.max().date() <= as_of.date() for frame in seen.values())
        assert seen["UP.US"].index.max().date() == as_of.date()


# ---- parity with the tick ---------------------------------------------------------


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield lake_trending, state, registry
    state.close()


def test_backtest_and_tick_build_the_same_target_book(tick_env, monkeypatch):
    lake, state, registry = tick_env
    strategies = {
        "bh_up": BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        "bh_down": BuyAndHold({"ticker": "DOWN.US", "allocation": 1.0}),
    }
    for sid, strategy in strategies.items():
        registry.register(
            strategy,
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
            strategy_id=sid,
        )
        seed_status(registry, sid, "active")
    people = People(state)
    people.book(
        people.trader("Alice"),
        "Parity",
        {"bh_up": 1.0, "bh_down": 1.0},
        construction={"method": "inverse_vol"},
    )
    captured = []

    class Capture(PostTickHook):
        name, stage = "capture_targets", "portfolio"

        def run(self, ctx):
            captured.append(dict(ctx.pipeline.target_book.weights))

    monkeypatch.setitem(hooks_mod._HOOKS, "capture_targets", Capture)
    day = date(2026, 3, 20)
    settings = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)
    run_tick(state, lake, registry, settings, as_of=day, plan=load_tick_plan(state, settings))

    engine, _, _ = _run(
        lake, list(strategies.values()), construction="inverse_vol", start=day, end=day
    )
    [tick_weights] = captured
    [book] = engine.target_books.values()
    assert set(tick_weights) == {"UP.US", "DOWN.US"}
    assert book.weights == pytest.approx(tick_weights, rel=1e-12)
    assert tick_weights["UP.US"] != pytest.approx(tick_weights["DOWN.US"])
