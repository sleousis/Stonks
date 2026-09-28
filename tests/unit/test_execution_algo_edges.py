"""The ExecutionAlgo seam at its edges (roadmap 23.16): cost assumptions
that are not numbers, slice rounding that leaves empty slices, windows that
are empty, the registry refusing a nameless or doubled algo, and IBKR's
native form before a window is resolved."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from stonks.execution.algos import AlgoParamsError, algo_spec, cost_assumption_of, get_algo
from stonks.execution.algos.base import (
    AlgoCostAssumption,
    AlgoWindow,
    ExecutionAlgo,
    WindowParams,
    register_algo,
    session_window,
    slices_over,
    split_whole,
)
from stonks.execution.algos.twap import Twap

OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "kw",
    [
        {"spread_factor": -0.1},
        {"impact_factor": math.inf},
        {"spread_factor": math.nan},
        {"timing_bps": math.inf},
        {"timing_bps": math.nan},
    ],
)
def test_a_cost_assumption_must_be_finite_and_non_negative(kw):
    with pytest.raises(ValueError, match="must be"):
        AlgoCostAssumption(**kw)


def test_a_negative_timing_cost_is_allowed():
    assert AlgoCostAssumption(timing_bps=-1.5).timing_bps == -1.5


def test_split_whole_refuses_negative_quantities_and_empty_weights():
    with pytest.raises(ValueError, match=">= 0"):
        split_whole(-1, [1.0])
    with pytest.raises(ValueError, match="positive"):
        split_whole(5, [])
    with pytest.raises(ValueError, match="positive"):
        split_whole(5, [0.0, 0.0])
    assert split_whole(0, [1.0, 1.0]) == [0, 0]


def test_slices_drop_empty_parts_and_renumber_from_one():
    window = AlgoWindow(OPEN, OPEN + timedelta(hours=3))
    out = slices_over(2, window, [1.0, 1.0, 1.0])
    assert [(s.seq, s.quantity) for s in out] == [(1, 1), (2, 1)]
    assert sum(s.quantity for s in out) == 2
    assert out[0].send_after == OPEN and out[1].send_after == OPEN + timedelta(hours=1)


def test_the_session_window_takes_params_as_a_model_and_refuses_an_empty_one():
    window = session_window(WindowParams(start_minutes=30, end_minutes=60), OPEN, CLOSE)
    assert (window.start, window.end) == (OPEN + timedelta(minutes=30), OPEN + timedelta(hours=1))
    with pytest.raises(AlgoParamsError, match="empty"):
        session_window({"start_minutes": 500}, OPEN, CLOSE)  # starts after the close


def test_twap_refuses_an_end_before_its_start():
    with pytest.raises(AlgoParamsError, match="end_minutes must be after"):
        algo_spec("twap", {"start_minutes": 60, "end_minutes": 60})


def test_twap_and_vwap_costs_and_native_form_without_a_window():
    assert cost_assumption_of("twap").impact_factor == 0.6
    twap = get_algo("twap").native(algo_spec("twap")["params"], None)
    assert twap.strategy == "Twap"
    assert dict(twap.params) == {"strategyType": "Marketable", "allowPastEndTime": "0"}
    vwap = get_algo("vwap").native(algo_spec("vwap")["params"], None)
    assert vwap.strategy == "Vwap"
    assert "startTime" not in dict(vwap.params) and dict(vwap.params)["maxPctVol"] == "0.1"


def test_an_algo_that_cannot_slice_says_so():
    with pytest.raises(NotImplementedError, match="adaptive cannot be sent as slices"):
        get_algo("adaptive").slices(10, {}, AlgoWindow(OPEN, CLOSE))


def test_the_registry_refuses_a_nameless_or_doubled_algo():
    class Nameless(Twap):
        name = ""

    with pytest.raises(ValueError, match="needs a name"):
        register_algo(Nameless)

    class Impostor(Twap):
        name = "twap"

    with pytest.raises(ValueError, match="already registered"):
        register_algo(Impostor)
    assert type(get_algo("twap")) is Twap  # the real one stays
    register_algo(Twap)  # the same class again is harmless
    assert type(get_algo("twap")) is Twap


def test_an_algo_is_an_abstract_seam():
    with pytest.raises(TypeError):
        ExecutionAlgo()  # type: ignore[abstract]
