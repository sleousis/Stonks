"""The signal phase (BL-12, W2.1): every strategy is scored once per tick,
and the instance that scored is the one that decides."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.core.types import Order
from stonks.production.ranker import Ranker, StrategyPool
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status

AS_OF = date(2026, 3, 20)
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]


class RemembersItsScores(BuyAndHold):
    """Buys its ticker only when this very instance scored it today, like
    StocksOnTheMove, whose ``decide`` reads what ``estimate_return``
    computed. Module scope so the registry can import it."""

    parallel_scoring = False  # decide reads what this instance scored

    def __init__(self, params):
        super().__init__(params)
        self.scored: set[tuple[str, str]] = set()
        self.entries: list[str] = []

    def estimate_return(self, ticker, as_of, lake):
        r = super().estimate_return(ticker, as_of, lake)
        if r is not None:
            self.scored.add((as_of.isoformat(), ticker))
        return r

    def decide(self, my_picks, portfolio, prices, as_of):
        target = self.params["ticker"]
        if (as_of.isoformat(), target) not in self.scored:
            return []
        self.entries.append(target)  # per-book state a shared instance would leak
        return [
            Order(
                client_id="x",
                ticker=target,
                side="buy",
                quantity=portfolio.cash * float(self.params["allocation"]) / prices[target],
            )
        ]


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield lake_trending, state, registry
    state.close()


def _register(registry, strategy, status="active", sid=None):
    sid = registry.register(
        strategy, reports=[SurvivalReport(test_id="oos", passed=True, metrics={})], strategy_id=sid
    )
    if status != "shadow":
        seed_status(registry, sid, status)
    return sid


def test_score_keeps_one_map_per_strategy_and_the_instances(env):
    lake, _, registry = env
    bh = _register(registry, BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), sid="bh")
    mom = _register(
        registry, Momentum({"lookback_days": 5, "threshold": 0.5, "allocation": 1.0}), sid="mom"
    )
    ranker = Ranker(registry=registry, lake=lake, universe=UNIVERSE, threshold=0.0)
    signals = ranker.score(AS_OF)

    assert list(signals.scores) == [bh, mom]
    assert signals.scores[bh] == {"UP.US": 1.0}
    assert signals.scores[mom] == {}  # scored, no opinion above its own threshold
    assert set(signals.instances) == {bh, mom}
    assert signals.ranked() == ranker.rank(AS_OF)


def test_pool_hands_out_the_scoring_instance_then_pristine_forks(env):
    lake, _, registry = env
    sid = _register(registry, RemembersItsScores({"ticker": "UP.US", "allocation": 0.5}))
    signals = Ranker(registry=registry, lake=lake, universe=UNIVERSE).score(AS_OF)
    pool = StrategyPool(registry, lake)
    pool.add(signals)
    pool.expect(sid, 2)

    first = pool.checkout(sid)
    assert first is signals.instances[sid]
    first.entries.append("mutated after checkout")
    second = pool.checkout(sid)
    assert second is not first
    assert second.scored == first.scored  # the day's evaluation survives
    assert second.entries == []  # but nothing the first consumer did


def test_pool_loads_unscored_strategies_and_surfaces_load_errors(env):
    lake, state, registry = env
    sid = _register(registry, BuyAndHold({"ticker": "UP.US", "allocation": 1.0}))
    pool = StrategyPool(registry, lake)
    assert pool.checkout(sid).params["ticker"] == "UP.US"
    state.execute("UPDATE strategies SET class_path = 'stonks.gone:Gone' WHERE id = ?", [sid])
    with pytest.raises(Exception, match="gone"):
        StrategyPool(registry, lake).checkout(sid)


def test_tick_decides_with_the_instance_that_scored(env):
    """Regression: the tick used to rank with one instance and decide with a
    freshly loaded one, so per-day state from estimate_return was lost and
    StocksOnTheMove never bought in production."""
    lake, state, registry = env
    _register(registry, RemembersItsScores({"ticker": "UP.US", "allocation": 0.5}))
    settings = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)

    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.orders_placed == 1
    assert result.fills == 1
    [order] = state.sql("SELECT ticker, side, status FROM orders")
    assert tuple(order) == ("UP.US", "buy", "filled")


def test_shadow_book_decides_with_the_instance_that_scored(env):
    lake, state, registry = env
    _register(registry, RemembersItsScores({"ticker": "UP.US", "allocation": 0.5}), "shadow")
    settings = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)

    run_tick(state, lake, registry, settings, as_of=AS_OF)

    rows = state.sql("SELECT ticker, side, status FROM shadow_decisions")
    assert [tuple(r) for r in rows] == [("UP.US", "buy", "filled")]


def test_model_books_for_every_strategy_share_the_scoring_instance(env):
    """``model_books="all"``: an active strategy trades the real book and
    keeps its own model book; both decide from the same day's evaluation,
    and neither sees what the other decided."""
    lake, state, registry = env
    sid = _register(registry, RemembersItsScores({"ticker": "UP.US", "allocation": 0.5}))
    settings = TickSettings(universe=UNIVERSE, initial_cash=10_000.0, model_books="all")

    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.fills == 1
    rows = state.sql("SELECT strategy_id, ticker, side, status FROM shadow_decisions")
    assert [tuple(r) for r in rows] == [(sid, "UP.US", "buy", "filled")]
