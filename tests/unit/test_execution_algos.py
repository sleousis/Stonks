"""The ExecutionAlgo seam (roadmap 23.16): registry, parameters, windows,
IBKR's native form, Stonks' child slices and the cost assumptions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.execution.algos import (
    AlgoParamsError,
    algo_names,
    algo_spec,
    cost_assumption_of,
    get_algo,
    native_of,
    route_for,
    window_of,
    with_window,
)
from stonks.execution.algos.base import AlgoCostAssumption, AlgoWindow, split_whole
from stonks.execution.algos.vwap import volume_curve

OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)


def test_registry_discovers_the_three_algos():
    assert algo_names() == ["adaptive", "twap", "vwap"]


def test_unknown_algo_is_refused():
    with pytest.raises(AlgoParamsError, match="unknown execution algo"):
        get_algo("iceberg")


def test_params_default_and_validate():
    assert algo_spec("adaptive") == {"name": "adaptive", "params": {"priority": "normal"}}
    assert algo_spec("twap", {"slices": 4})["params"] == {
        "start_minutes": 0,
        "end_minutes": None,
        "slices": 4,
    }
    with pytest.raises(AlgoParamsError, match="priority"):
        algo_spec("adaptive", {"priority": "whenever"})
    with pytest.raises(AlgoParamsError, match="end_minutes must be after"):
        algo_spec("vwap", {"start_minutes": 60, "end_minutes": 30})
    with pytest.raises(AlgoParamsError):
        algo_spec("vwap", {"max_participation": 0.9})
    with pytest.raises(AlgoParamsError, match="Extra"):
        algo_spec("twap", {"bogus": 1})


def test_window_resolves_inside_the_session():
    spec = with_window(algo_spec("vwap", {"start_minutes": 30, "end_minutes": 120}), OPEN, CLOSE)
    window = window_of(spec)
    assert window == AlgoWindow(OPEN + timedelta(minutes=30), OPEN + timedelta(minutes=120))
    # past the close: clipped to the close
    late = with_window(algo_spec("twap", {"end_minutes": 900}), OPEN, CLOSE)
    assert window_of(late).end == CLOSE  # type: ignore[union-attr]
    with pytest.raises(AlgoParamsError, match="empty"):
        with_window(algo_spec("twap", {"start_minutes": 400}), OPEN, CLOSE)


def test_adaptive_has_no_window():
    spec = with_window(algo_spec("adaptive", {"priority": "patient"}), OPEN, CLOSE)
    assert "window" not in spec
    native = native_of(spec)
    assert native.strategy == "Adaptive"
    assert native.params == (("adaptivePriority", "Patient"),)


def test_native_vwap_and_twap_carry_ibkr_tags():
    vwap = native_of(with_window(algo_spec("vwap", {"end_minutes": 60}), OPEN, CLOSE))
    assert vwap.strategy == "Vwap"
    tags = dict(vwap.params)
    assert tags["maxPctVol"] == "0.1"
    assert tags["startTime"] == "20260929-13:30:00"
    assert tags["endTime"] == "20260929-14:30:00"
    assert tags["allowPastEndTime"] == "0"
    twap = native_of(with_window(algo_spec("twap"), OPEN, CLOSE))
    assert twap.strategy == "Twap"
    assert dict(twap.params)["strategyType"] == "Marketable"
    assert dict(twap.params)["endTime"] == "20260929-20:00:00"


def test_split_whole_sums_exactly():
    assert split_whole(10, [1, 1, 1]) == [4, 3, 3]
    assert sum(split_whole(1001, volume_curve(13))) == 1001
    assert split_whole(0, [1, 2]) == [0, 0]
    with pytest.raises(ValueError):
        split_whole(5, [])


def test_twap_slices_are_equal_and_evenly_spaced():
    window = AlgoWindow(OPEN, OPEN + timedelta(minutes=60))
    slices = get_algo("twap").slices(100, {"slices": 4}, window)
    assert [s.quantity for s in slices] == [25, 25, 25, 25]
    assert [s.send_after for s in slices] == [OPEN + timedelta(minutes=15 * i) for i in range(4)]
    assert [s.seq for s in slices] == [1, 2, 3, 4]


def test_small_orders_get_fewer_slices_never_empty_ones():
    window = AlgoWindow(OPEN, CLOSE)
    slices = get_algo("twap").slices(3, {"slices": 6}, window)
    assert [s.quantity for s in slices] == [1, 1, 1]
    vwap = get_algo("vwap").slices(2, {"slices": 13}, window)
    assert sum(s.quantity for s in vwap) == 2
    assert all(s.quantity > 0 for s in vwap)


def test_vwap_slices_follow_the_u_shaped_curve():
    window = AlgoWindow(OPEN, CLOSE)
    slices = get_algo("vwap").slices(1300, {"slices": 13}, window)
    sizes = [s.quantity for s in slices]
    assert sum(sizes) == 1300
    assert sizes[0] > sizes[6] < sizes[-1]


def test_routes():
    ibkr = frozenset({"adaptive", "vwap", "twap"})
    assert route_for("vwap", ibkr) == "native"
    assert route_for("vwap", frozenset()) == "sliced"
    assert route_for("twap", frozenset()) == "sliced"
    assert route_for("adaptive", frozenset()) == "plain"


def test_cost_assumptions():
    assert cost_assumption_of("adaptive", {"priority": "patient"}).spread_factor < (
        cost_assumption_of("adaptive", {"priority": "urgent"}).spread_factor
    )
    assert cost_assumption_of("vwap").impact_factor < 1.0
    with pytest.raises(ValueError):
        AlgoCostAssumption(spread_factor=-1)


def test_describe_lists_schema_defaults_and_costs():
    info = get_algo("vwap").describe()
    assert info["name"] == "vwap"
    assert info["sliceable"] is True
    assert "max_participation" in info["params_schema"]["properties"]
    assert info["defaults"]["slices"] == 13
    assert info["cost_assumption"]["impact_factor"] == 0.5


def test_window_needs_aware_times():
    with pytest.raises(ValueError):
        AlgoWindow(datetime(2026, 1, 1), datetime(2026, 1, 2))
    with pytest.raises(ValueError):
        AlgoWindow(CLOSE, OPEN)
