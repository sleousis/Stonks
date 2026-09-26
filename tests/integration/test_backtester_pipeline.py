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


# ---- RS-04: the trade ledger pairs lots per ticker in pipeline runs ---------------


class _Window(BuyAndHold):
    """Scores ``ticker`` 1.0 on days inside [first, last], nothing otherwise."""

    id = "window_fake"

    def __init__(self, ticker: str, first: date, last: date) -> None:
        super().__init__({"ticker": ticker, "allocation": 1.0})
        self._first, self._last = first, last

    def estimate_return(self, ticker, as_of, lake):
        day = as_of.date() if hasattr(as_of, "date") else as_of
        return 1.0 if ticker == self.params["ticker"] and self._first <= day <= self._last else None


def _ledger(lake, strategies, **config):
    from stonks.backtest.trades import with_trades

    _, report, broker = _run(lake, strategies, **config)
    return with_trades(report, broker.fills, reference_price=broker.reference_price), broker


def test_owner_change_between_buy_and_sell_closes_the_lot(lake_trending):
    report, broker = _ledger(
        lake_trending,
        [
            _Window("UP.US", date(2026, 1, 5), date(2026, 1, 20)),
            _Window("UP.US", date(2026, 1, 12), date(2026, 2, 10)),
        ],
        construction="equal_weight_top_n",
    )
    keys = {f.order_client_id.partition(":")[0] for f in broker.fills}
    assert len(keys) == 2, keys  # the owner flipped between the buy and the sell
    assert broker.fetch_portfolio().positions.get("UP.US", 0.0) == pytest.approx(0.0)
    assert report.trades and not any(t.is_open for t in report.trades)
    pnl = sum(t.pnl for t in report.trades)
    assert pnl == pytest.approx(report.equity_curve[-1] - report.equity_curve[0], rel=1e-9)


def test_max_holding_exit_closes_the_lot(lake_trending):
    from stonks.production.rules.max_holding import MaxHoldingSettings
    from stonks.production.rules.settings import RuleSettings

    report, broker = _ledger(
        lake_trending,
        [_Window("UP.US", date(2026, 1, 5), date(2026, 2, 27))],
        construction="equal_weight_top_n",
        risk=RiskPolicy(rules=RuleSettings(max_holding=MaxHoldingSettings(max_holding_bars=3))),
    )
    sells = [f for f in broker.fills if f.side == "sell"]
    assert any(not f.order_client_id.startswith("0:") for f in sells), sells
    assert not any(t.is_open for t in report.trades)
    pnl = sum(t.pnl for t in report.trades)
    assert pnl == pytest.approx(report.equity_curve[-1] - report.equity_curve[0], rel=1e-9)


# ---- RS-15: pipeline risk sizing ignores splits after the decision ---------------


def _split_lake(path, *, future_split: bool):
    """X.US with a noisy daily path. ``future_split`` stamps the vendor's
    adj_close as if a 10:1 split happened after the window (every bar /10)."""
    import numpy as np
    import pandas as pd

    from stonks.store.lake import DuckDBLake

    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2025-09-01", "2026-03-31")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(dates))))
    lake = DuckDBLake(path)
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "X.US",
                "date": [d.date() for d in dates],
                "open": close,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "adj_close": close / 10.0 if future_split else close,
                "volume": 1e6,
            }
        )
    )
    lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES ('X.US', 'equity')")
    return lake


def test_risk_sizing_is_the_same_with_and_without_a_future_split(tmp_path):
    from stonks.production.rules.risk_per_position import RiskPerPositionSettings
    from stonks.production.rules.settings import RuleSettings

    risk = RiskPolicy(
        rules=RuleSettings(risk_per_position=RiskPerPositionSettings(max_risk=0.0025))
    )
    quantities = []
    for future in (False, True):
        lake = _split_lake(tmp_path / f"lake{future}.duckdb", future_split=future)
        broker = SimulatedBroker(Portfolio(cash=10_000.0))
        Backtester(
            strategies=[_Window("X.US", date(2026, 1, 5), date(2026, 3, 20))],
            broker=broker,
            lake=lake,
            config=BacktestConfig(
                start=date(2026, 1, 5),
                end=date(2026, 3, 20),
                universe=["X.US"],
                construction="equal_weight_top_n",
                risk=risk,
            ),
        ).run()
        quantities.append([round(f.quantity, 6) for f in broker.fills])
        lake.close()
    assert quantities[0], "the risk rule must still let a buy through"
    assert quantities[1] == quantities[0]
