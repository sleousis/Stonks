"""Execution algorithms (roadmap 23.16): how one parent order is worked.

See :mod:`stonks.execution.algos.base` for the seam. Each public module
other than ``base`` and ``settings`` registers one
algo."""

from stonks.execution.algos.base import (
    AlgoCostAssumption,
    AlgoParamsError,
    AlgoRoute,
    AlgoWindow,
    ChildSlice,
    ExecutionAlgo,
    NativeAlgo,
    algo_name_of,
    algo_names,
    algo_spec,
    cost_assumption_of,
    get_algo,
    native_of,
    register_algo,
    route_for,
    window_of,
    with_window,
)

__all__ = [
    "AlgoCostAssumption",
    "AlgoParamsError",
    "AlgoRoute",
    "AlgoWindow",
    "ChildSlice",
    "ExecutionAlgo",
    "NativeAlgo",
    "algo_name_of",
    "algo_names",
    "algo_spec",
    "cost_assumption_of",
    "get_algo",
    "native_of",
    "register_algo",
    "route_for",
    "window_of",
    "with_window",
]
