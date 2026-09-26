"""LastTradeFilter: admit an inner strategy's entry only after a loser (or winner)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.core.types import Portfolio
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.last_trade_filter import LastTradeFilter
from tests.unit.nt888_helpers import SCRIPTED, STUB, FittedStub, make_lake, write_bars

ON, OFF = 3e6, 1e6
# day:        0     1     2     3     4     5     6     7     8     9
VOLUMES = [OFF, ON, ON, OFF, ON, ON, OFF, ON, ON, OFF]
CLOSES = [100, 100, 105, 110, 110, 111, 100, 100, 101, 101]
# trades: enter d1 @100 -> exit d3 @110 (winner); enter d4 @110 -> exit d6 @100 (loser);
#         enter d7 @100 (open until d9)
DATES = pd.bdate_range("2024-01-01", periods=len(CLOSES))


def day(i):
    return DATES[i].to_pydatetime()


@pytest.fixture
def lake(tmp_path):
    db = make_lake(tmp_path / "lake.duckdb")
    write_bars(db, "X.US", DATES, np.array(CLOSES, float), np.array(VOLUMES, float))
    yield db
    db.close()


def _filter(**overrides):
    return LastTradeFilter(
        {
            "inner_class_path": SCRIPTED,
            "inner_params": {"ticker": "X.US"},
            **overrides,
        }
    )


def _admitted(f, lake):
    return [f.estimate_return("X.US", day(i), lake) is not None for i in range(len(CLOSES))]


def test_surface():
    f = _filter()
    assert isinstance(f, Strategy)
    assert f.params["require_last"] == "loser"
    assert f.id.startswith("scripted")
    assert f.applicable_asset_classes == ("crypto", "equity")


def test_after_a_loser_mode(lake):
    #                         0      1      2      3      4      5      6      7     8     9
    assert _admitted(_filter(), lake) == [
        False,  # inner off
        False,  # first entry: no completed trade yet
        False,
        False,
        False,  # previous trade was a winner
        False,
        False,
        True,  # previous trade was a loser
        True,  # still the admitted trade
        False,  # inner exits
    ]


def test_after_a_winner_mode(lake):
    admitted = _admitted(_filter(require_last="winner"), lake)
    assert admitted == [False, False, False, False, True, True, False, False, False, False]


def test_admitted_estimate_is_the_inner_estimate(lake):
    assert _filter().estimate_return("X.US", day(7), lake) == pytest.approx(0.01)


def test_other_tickers_pass_through_as_none(lake):
    assert _filter().estimate_return("Y.US", day(7), lake) is None


def test_replay_window_is_bounded_and_ignores_a_trade_already_open_at_its_start(lake):
    # bars 5..7: bar 5 is mid-trade (entry unknown) so no completed trade is
    # known when bar 7 enters
    f = _filter(replay_bars=3)
    assert f.estimate_return("X.US", day(7), lake) is None
    assert _filter(replay_bars=5).estimate_return("X.US", day(7), lake) is not None


def test_replay_is_causal(tmp_path, lake):
    other = make_lake(tmp_path / "future.duckdb")
    try:
        closes = np.array(CLOSES, float)
        volumes = np.array(VOLUMES, float)
        closes[6:] = 500.0
        volumes[6:] = ON
        write_bars(other, "X.US", DATES, closes, volumes)
        for i in range(6):
            assert _filter().estimate_return("X.US", day(i), lake) == _filter().estimate_return(
                "X.US", day(i), other
            )
    finally:
        other.close()


def test_inner_signals_are_memoized_per_bar(lake):
    f = _filter()
    for i in range(len(CLOSES)):
        f.estimate_return("X.US", day(i), lake)
    # one call per bar for the replay, plus one live call per as_of
    assert f.inner.calls <= 2 * len(CLOSES)


def test_fit_forgets_memoized_inner_signals(lake):
    f = _filter()
    f.estimate_return("X.US", day(8), lake)
    calls = f.inner.calls
    f.fit(None)
    f.estimate_return("X.US", day(8), lake)
    assert f.inner.calls - calls > len(CLOSES) // 2


def test_exits_pass_through(lake):
    f = _filter()
    f.estimate_return("X.US", day(9), lake)
    orders = f.decide([], Portfolio(cash=0.0, positions={"X.US": 3.0}), {"X.US": 101.0}, day(9))
    assert [(o.side, o.ticker, o.quantity) for o in orders] == [("sell", "X.US", 3.0)]


def test_admitted_pick_buys(lake):
    f = _filter()
    est = f.estimate_return("X.US", day(7), lake)
    orders = f.decide([(est, "X.US")], Portfolio(cash=1000.0), {"X.US": 100.0}, day(7))
    assert [(o.side, o.ticker) for o in orders] == [("buy", "X.US")]


def test_registry_round_trip_restores_inner_state(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        f = _filter(inner_class_path=STUB, inner_params={"ticker": "X.US"}, require_last="winner")
        f.fit(None)
        sid = registry.register(f, [SurvivalReport(test_id="oos", passed=True, metrics={})])
        loaded = registry.load(sid)
        assert isinstance(loaded, LastTradeFilter)
        assert isinstance(loaded.inner, FittedStub)
        assert loaded.inner.fitted_value == 42.0
        assert loaded.params == f.params
    finally:
        state.close()
