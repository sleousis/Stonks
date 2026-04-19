"""Integration tests for the production Ranker — it iterates active
strategies × tickers × prices and builds a sorted list of (expected_return,
strategy_handle, ticker) candidates above a threshold.
"""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.ranker import Ranker
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum


@pytest.fixture
def seeded_registry(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")

    bh_id = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})],
    )
    mom_id = registry.register(
        Momentum({"lookback_days": 20, "threshold": 0.0, "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 0.5})],
    )
    # Mark both active
    registry.set_status(bh_id, "active")
    registry.set_status(mom_id, "active")

    yield registry, lake_trending, [bh_id, mom_id]
    state.close()


def test_ranker_produces_sorted_picks_above_threshold(seeded_registry):
    registry, lake, _ = seeded_registry
    ranker = Ranker(
        registry=registry,
        lake=lake,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        threshold=0.0,
    )
    picks = ranker.rank(as_of=date(2026, 3, 20))
    assert picks, "expected at least one candidate above threshold"
    # sorted descending by expected_return
    returns = [r for r, *_ in picks]
    assert returns == sorted(returns, reverse=True)
    # everything above threshold
    assert all(r > 0.0 for r in returns)


def test_ranker_returns_empty_when_no_active_strategies(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        ranker = Ranker(
            registry=registry,
            lake=lake_trending,
            universe=["UP.US"],
            threshold=0.0,
        )
        assert ranker.rank(as_of=date(2026, 3, 20)) == []
    finally:
        state.close()


def test_ranker_respects_threshold(seeded_registry):
    registry, lake, _ = seeded_registry
    # a very high threshold should prune to 0 picks
    ranker = Ranker(
        registry=registry,
        lake=lake,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        threshold=100.0,
    )
    assert ranker.rank(as_of=date(2026, 3, 20)) == []
