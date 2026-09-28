"""The replay of recent sessions before a book starts, restarts or moves up
a stage (roadmap 23.15). It blocks on zero decisions or all errors and never
looks at profit or loss."""

from __future__ import annotations

from datetime import date

import pandas as pd

from stonks.production.live.replay import replay_check, replay_strategies
from stonks.production.live.settings import ReplaySettings

DAYS = pd.bdate_range("2026-09-14", "2026-09-25")


def seed(lake, tickers=("A.US", "B.US")):
    rows = [
        {"ticker": t, "date": d.date(), "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.0 + i, "adj_close": 10.0 + i, "volume": 1000}
        for t in tickers
        for i, d in enumerate(DAYS)
    ]  # fmt: skip
    lake.upsert_prices(pd.DataFrame(rows))
    for t in tickers:
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [t])


class Scores:
    applicable_asset_classes = ("equity",)

    def __init__(self, value=0.1, fail=False, seen=None):
        self.value, self.fail, self.seen = value, fail, seen

    def estimate_return(self, ticker, as_of, lake):
        if self.seen is not None:
            self.seen.append(as_of)
        if self.fail:
            raise RuntimeError("broken feature")
        return self.value


AS_OF = date(2026, 9, 25)


def test_a_strategy_that_decides_passes(lake):
    seed(lake)
    report = replay_strategies(lake, {"s1": Scores}, ["A.US", "B.US"], AS_OF, sessions=3)
    assert report.passed is True
    [s] = report.strategies
    assert (s.sessions, s.calls, s.decisions, s.errors) == (3, 6, 6, 0)
    assert [d.isoformat() for d in report.sessions] == ["2026-09-23", "2026-09-24", "2026-09-25"]


def test_blocks_on_zero_decisions_and_on_all_errors(lake):
    seed(lake)
    loaders = {"quiet": lambda: Scores(value=None), "broken": lambda: Scores(fail=True)}
    report = replay_strategies(lake, loaders, ["A.US", "B.US"], AS_OF, sessions=2)
    assert report.passed is False
    blockers = report.blockers()
    assert any("quiet" in b and "no decision" in b for b in blockers)
    assert any("broken" in b and "every call failed" in b for b in blockers)


def test_some_errors_do_not_block(lake):
    seed(lake)

    class Half(Scores):
        def estimate_return(self, ticker, as_of, lake):
            if ticker == "A.US":
                raise RuntimeError("no data")
            return -0.5  # a losing view is still a decision

    report = replay_strategies(lake, {"half": Half}, ["A.US", "B.US"], AS_OF, sessions=2)
    assert report.passed is True
    assert report.strategies[0].errors == 2


def test_a_strategy_that_fails_to_load_blocks(lake):
    seed(lake)

    def boom():
        raise ImportError("renamed class")

    report = replay_strategies(lake, {"gone": boom}, ["A.US"], AS_OF, sessions=2)
    assert report.passed is False and "renamed class" in report.blockers()[0]


def test_nothing_to_replay_is_unknown_and_does_not_block(lake):
    assert replay_strategies(lake, {"s1": Scores}, ["A.US"], AS_OF, sessions=2).passed is None
    seed(lake)
    assert replay_strategies(lake, {}, ["A.US"], AS_OF, sessions=2).passed is None
    assert replay_strategies(lake, {"s1": Scores}, [], AS_OF, sessions=2).passed is None


def test_only_sessions_on_or_before_the_day_are_replayed(lake):
    seed(lake)
    seen: list[date] = []
    replay_strategies(
        lake, {"s1": lambda: Scores(seen=seen)}, ["A.US"], date(2026, 9, 17), sessions=5
    )
    assert max(seen) == date(2026, 9, 17)


def test_replay_check_reads_the_registry_and_can_be_switched_off(lake):
    seed(lake)

    class Registry:
        def load(self, sid):
            return Scores()

    on = replay_check(lake, Registry(), ["s1"], ["A.US"], AS_OF, ReplaySettings(sessions=2))
    assert on.passed is True
    off = replay_check(lake, Registry(), ["s1"], ["A.US"], AS_OF, ReplaySettings(enabled=False))
    assert off.passed is None and "off" in off.detail
