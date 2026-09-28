"""Algo settings per portfolio and strategy, and a parent's state from its
children (roadmap 23.16)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.core.types import Order
from stonks.execution.algos import AlgoParamsError
from stonks.execution.algos.base import ChildSlice
from stonks.execution.algos.settings import (
    attach_algos,
    clear_setting,
    list_settings,
    resolve_algo,
    set_setting,
    works_with_algo,
)
from stonks.production.algo_slices import child_client_id, child_order, parent_state_of
from stonks.store.state import SqliteState

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def order(**kw) -> Order:
    base = {"client_id": "c1", "ticker": "AAPL.US", "side": "buy", "quantity": 10.0,
            "strategy_id": "s1"}  # fmt: skip
    base.update(kw)
    return Order(**base)


def test_no_setting_means_plain(state):
    assert resolve_algo(state, "pf", "s1") is None
    assert attach_algos(state, [order()], "pf")[0].algo is None


def test_portfolio_and_strategy_settings(state):
    set_setting(state, "pf", "adaptive", {"priority": "patient"}, actor="u", now=NOW)
    set_setting(state, "pf", "vwap", {"slices": 4}, strategy_id="s2", actor="u", now=NOW)
    assert resolve_algo(state, "pf", "s1") == {
        "name": "adaptive",
        "params": {"priority": "patient"},
    }
    assert resolve_algo(state, "pf", "s2")["name"] == "vwap"  # type: ignore[index]
    assert resolve_algo(state, "pf", None)["name"] == "adaptive"  # type: ignore[index]
    rows = list_settings(state, "pf")
    assert [(r.strategy_id, r.algo) for r in rows] == [(None, "adaptive"), ("s2", "vwap")]
    # an update replaces, a clear falls back
    set_setting(state, "pf", "twap", {}, actor="u", now=NOW)
    assert resolve_algo(state, "pf", "s1")["name"] == "twap"  # type: ignore[index]
    assert clear_setting(state, "pf", "s2")
    assert resolve_algo(state, "pf", "s2")["name"] == "twap"  # type: ignore[index]
    assert not clear_setting(state, "pf", "s2")


def test_bad_params_are_refused(state):
    with pytest.raises(AlgoParamsError):
        set_setting(state, "pf", "vwap", {"max_participation": 2}, actor="u", now=NOW)
    assert list_settings(state, "pf") == []


def test_stops_options_and_oca_orders_stay_plain(state):
    set_setting(state, "pf", "twap", {}, actor="u", now=NOW)
    stop = order(client_id="c2", order_type="stop", stop_price=90.0)
    oca = order(client_id="c3", oca_group="g1")
    option = order(client_id="c4", ticker="AAPL.US:2026-12-18:C:200")
    kept = order(client_id="c5", algo={"name": "adaptive", "params": {"priority": "urgent"}})
    out = attach_algos(state, [order(), stop, oca, option, kept], "pf")
    assert out[0].algo["name"] == "twap"  # type: ignore[index]
    assert [o.algo for o in out[1:4]] == [None, None, None]
    assert out[4].algo["name"] == "adaptive"  # type: ignore[index]
    assert not works_with_algo(stop)


def test_child_order_is_a_tagged_day_order():
    parent = order(algo={"name": "twap", "params": {}, "window": {}}, time_in_force="opg")
    child = child_order(parent, ChildSlice(seq=2, quantity=3, send_after=NOW))
    assert child.client_id == child_client_id("c1", 2) == "c1.s02"
    assert (child.quantity, child.time_in_force) == (3.0, "day")
    assert child.algo == {"name": "twap", "parent": "c1"}


@pytest.mark.parametrize(
    ("filled", "slices", "children", "cancelled", "expected"),
    [
        (0, ["planned", "planned"], [None, None], False, "accepted"),
        (5, ["sent", "planned"], ["filled", None], False, "partially_filled"),
        (10, ["sent", "sent"], ["filled", "filled"], False, "filled"),
        (5, ["sent", "skipped"], ["filled", None], False, "expired"),
        (0, ["skipped", "skipped"], [None, None], False, "expired"),
        (0, ["sent", "sent"], ["rejected", "rejected"], False, "rejected"),
        (0, ["sent", "sent"], ["cancelled", "expired"], False, "cancelled"),
        (0, ["sent"], ["accepted"], False, "accepted"),
        (5, ["sent", "skipped"], ["filled", None], True, "cancelled"),
    ],
)
def test_parent_state_from_children(filled, slices, children, cancelled, expected):
    assert parent_state_of(10, filled, slices, children, cancelled=cancelled) == expected
