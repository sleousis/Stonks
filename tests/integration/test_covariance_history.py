"""The tick and the backtest feed daily return history to the covariance
constructors (roadmap 9.5.1).

Three names with the same volatility: A and B move together, C on its own.
With history, HRP and ERC give C more than A or B. Without it (the old
behaviour) the three looked alike and each got a third. A planted future
that makes C a copy of A changes nothing at the decision (P12)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Portfolio
from stonks.production import hooks as hooks_mod
from stonks.production.hooks import PostTickHook
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status
from tests.integration.test_tick_portfolios import People

UNIVERSE = ["A.US", "B.US", "C.US"]
DATES = pd.bdate_range("2025-01-01", periods=320)
N_PAST = 300
DAY = DATES[N_PAST - 1].date()


def _lake(path: Path, *, future: bool) -> DuckDBLake:
    rng = np.random.default_rng(5)
    common = rng.normal(0.0005, 0.012, len(DATES))
    returns = {
        "A.US": common + rng.normal(0.0, 0.002, len(DATES)),
        "B.US": common + rng.normal(0.0, 0.002, len(DATES)),
        "C.US": rng.normal(0.0005, 0.0122, len(DATES)),
    }
    if future:
        returns["C.US"][N_PAST:] = returns["A.US"][N_PAST:]  # correlated from now on
    n = len(DATES) if future else N_PAST
    frames = []
    for ticker, r in returns.items():
        close = 100.0 * np.exp(np.cumsum(r))
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES[:n]],
                    "open": close[:n],
                    "high": close[:n],
                    "low": close[:n],
                    "close": close[:n],
                    "adj_close": close[:n],
                    "volume": 1_000_000.0,
                }
            )
        )
    lake = DuckDBLake(path)
    lake.migrate()
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    for ticker in UNIVERSE:
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    return lake


@pytest.fixture
def lakes(tmp_path):
    past = _lake(tmp_path / "past.duckdb", future=False)
    planted = _lake(tmp_path / "planted.duckdb", future=True)
    yield past, planted
    past.close()
    planted.close()


def _strategies():
    return {f"bh_{t[0].lower()}": BuyAndHold({"ticker": t, "allocation": 1.0}) for t in UNIVERSE}


def _backtest_weights(lake, method):
    engine = Backtester(
        strategies=list(_strategies().values()),
        broker=SimulatedBroker(Portfolio(cash=100_000.0)),
        lake=lake,
        config=BacktestConfig(start=DAY, end=DAY, universe=UNIVERSE, construction=method),
    )
    engine.run()
    [book] = engine.target_books.values()
    return {k[0]: v for k, v in book.weights.items()}, book.meta


def _tick_weights(tmp_path, lake, method, monkeypatch):
    folder = tmp_path / f"tick_{method}_{id(lake)}"
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
        "Covariance",
        dict.fromkeys(_strategies(), 1.0),
        construction={"method": method},
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


def _assert_diversifies(weights, meta):
    assert meta["covariance"] == "history"
    assert set(weights) == {"A", "B", "C"}
    assert weights["C"] > weights["A"] + 0.1
    assert weights["C"] > weights["B"] + 0.1
    assert weights["A"] == pytest.approx(weights["B"], abs=0.05)


@pytest.mark.parametrize("method", ["hrp", "erc"])
def test_backtest_weights_follow_correlation(lakes, method):
    past, _ = lakes
    _assert_diversifies(*_backtest_weights(past, method))


@pytest.mark.parametrize("method", ["hrp", "erc"])
def test_tick_weights_follow_correlation(lakes, method, tmp_path, monkeypatch):
    past, _ = lakes
    _assert_diversifies(*_tick_weights(tmp_path, past, method, monkeypatch))


@pytest.mark.parametrize("method", ["hrp", "erc"])
def test_tick_and_backtest_build_the_same_book(lakes, method, tmp_path, monkeypatch):
    past, _ = lakes
    tick, _ = _tick_weights(tmp_path, past, method, monkeypatch)
    backtest, _ = _backtest_weights(past, method)
    assert backtest == pytest.approx(tick, rel=1e-9)


@pytest.mark.parametrize("method", ["hrp", "erc"])
def test_no_row_after_the_decision_is_read(lakes, method, tmp_path, monkeypatch):
    past, planted = lakes
    assert _backtest_weights(planted, method) == _backtest_weights(past, method)
    assert _tick_weights(tmp_path, planted, method, monkeypatch)[0] == pytest.approx(
        _tick_weights(tmp_path, past, method, monkeypatch)[0], rel=1e-12
    )


def test_single_winner_reads_no_return_history(lakes, monkeypatch):
    """The default book loads nothing new: the history reader is never called."""
    import stonks.portfolio.returns as returns_mod

    def boom(*_args, **_kwargs):
        raise AssertionError("single_winner must not load return history")

    monkeypatch.setattr(returns_mod, "market_history", boom)
    past, _ = lakes
    engine = Backtester(
        strategies=[BuyAndHold({"ticker": "A.US", "allocation": 1.0})],
        broker=SimulatedBroker(Portfolio(cash=100_000.0)),
        lake=past,
        config=BacktestConfig(
            start=DATES[N_PAST - 20].date(),
            end=DAY,
            universe=UNIVERSE,
            construction="single_winner",
        ),
    )
    engine.run()
    assert engine.target_books
