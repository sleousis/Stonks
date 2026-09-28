"""The ranker and the strategy pool at their edges: a lake that cannot read
universe membership, a strategy that fails on one ticker, and a pool asked
for more instances than were declared."""

from __future__ import annotations

from typing import Any

from stonks.production.ranker import StrategyPool
from tests.unit.test_ranker_pit import AS_OF, _Lake, _ranker, _Recorder, _Registry


class _NoMembership(_Lake):
    def __getattribute__(self, name: str) -> Any:
        if name == "members_between":
            raise AttributeError(name)
        return super().__getattribute__(name)


def test_a_lake_without_membership_scores_the_whole_universe():
    strategy = _Recorder()
    signals = _ranker(_NoMembership(), strategy, universe_id="idx").score(AS_OF)
    assert sorted(signals.scores["s"]) == ["A.US", "B.US", "C.US"]


class _FailsOnB(_Recorder):
    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if ticker == "B.US":
            raise ValueError("no data for B")
        return 1.0 if ticker == "A.US" else None


def test_a_ticker_that_fails_is_skipped_and_the_others_scored():
    signals = _ranker(_Lake(), _FailsOnB()).score(AS_OF)
    assert signals.scores["s"] == {"A.US": 1.0}


class _Counter:
    applicable_asset_classes = ("equity",)

    def __init__(self) -> None:
        self.state: list[str] = []


def test_an_unexpected_consumer_gets_a_fresh_copy_not_the_scoring_instance():
    loads: list[str] = []

    class Registry(_Registry):
        def load(self, sid: str) -> Any:
            loads.append(sid)
            return _Counter()

    pool = StrategyPool(Registry({"s": None}), lake=object())  # type: ignore[arg-type]
    pool.expect("s", 1)
    first = pool.checkout("s")
    first.state.append("decided")  # the scoring instance now carries a decision
    second = pool.checkout("s")
    third = pool.checkout("s")
    assert second is not first and second.state == []
    assert third is not second and third.state == []
    assert loads == ["s", "s"]  # the first load, then one pristine copy from the registry
