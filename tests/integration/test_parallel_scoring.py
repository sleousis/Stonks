"""Parallel scoring (S5): opted-in strategies are scored in worker
processes over a read-only lake snapshot, with the serial path's scores."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production import scoring
from stonks.production.ranker import Ranker
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status
from tests.integration.test_signal_phase import RemembersItsScores

AS_OF = date(2026, 3, 20)
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]


@pytest.fixture
def registry(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    for sid, strategy in [
        ("mom", Momentum({"lookback_days": 5, "threshold": -1.0, "allocation": 0.5})),
        ("bh_up", BuyAndHold({"ticker": "UP.US", "allocation": 0.4})),
        ("remembers", RemembersItsScores({"ticker": "FLAT.US", "allocation": 0.4})),
    ]:
        registry.register(strategy, reports=reports, strategy_id=sid)
        seed_status(registry, sid, "active")
    yield registry
    state.close()


def _ranker(registry, lake, **kw):
    return Ranker(registry=registry, lake=lake, universe=UNIVERSE, threshold=-1.0, **kw)


def _plain(signals):
    return [(sid, list(scores.items())) for sid, scores in signals.scores.items()]


def test_workers_score_what_the_serial_path_scores(registry, lake_trending, monkeypatch):
    serial = _ranker(registry, lake_trending).score(AS_OF)
    calls = []
    real = scoring.score_in_workers

    def spy(lake, strategies, tickers, **kw):
        calls.append(sorted(strategies))
        return real(lake, strategies, tickers, **kw)

    monkeypatch.setattr(scoring, "score_in_workers", spy)
    parallel = _ranker(registry, lake_trending, workers=2, min_parallel_estimates=1).score(AS_OF)
    assert calls == [["bh_up", "mom"]]  # the stateful strategy stays in this process
    assert _plain(parallel) == _plain(serial)


def test_small_runs_and_single_worker_stay_serial(registry, lake_trending, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no pool expected")

    monkeypatch.setattr(scoring, "score_in_workers", boom)
    _ranker(registry, lake_trending, workers=4).score(AS_OF)  # 6 estimates < 2000
    _ranker(registry, lake_trending, workers=1, min_parallel_estimates=1).score(AS_OF)


def test_a_failing_pool_falls_back_to_serial(registry, lake_trending, monkeypatch):
    def boom(*a, **k):
        raise OSError("no processes today")

    monkeypatch.setattr(scoring, "score_in_workers", boom)
    serial = _ranker(registry, lake_trending).score(AS_OF)
    got = _ranker(registry, lake_trending, workers=2, min_parallel_estimates=1).score(AS_OF)
    assert _plain(got) == _plain(serial)


class _Picky(BuyAndHold):
    def estimate_return(self, ticker, as_of, lake):
        if ticker == "DOWN.US":
            raise ValueError("bad data")
        return 0.5


class _Portable:
    strategy = _Picky({"ticker": "UP.US", "allocation": 0.4})


def test_a_failing_estimate_drops_only_that_ticker(lake_trending):
    sid, out = scoring._score_chunk(lake_trending, ("p", _Portable(), UNIVERSE, 0.1, AS_OF))
    assert (sid, out) == ("p", {"UP.US": 0.5, "FLAT.US": 0.5})
    empty = scoring.score_in_workers(lake_trending, {}, {}, as_of=AS_OF, threshold=0, workers=2)
    assert empty == {}
