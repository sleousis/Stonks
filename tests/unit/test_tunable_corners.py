"""RS-29: every corner of every catalogued strategy's tunable space builds.

A corner that raises is a wasted trial, and it still counts towards the
trial total behind the deflated Sharpe."""

from __future__ import annotations

import itertools

import pytest

from stonks.core.params import tunable_only
from stonks.lab.catalog import strategy_catalog


def _corners(cls) -> list[dict]:
    axes = []
    for spec in tunable_only(cls.parameter_spec()):
        if spec.kind == "categorical":
            values = list(spec.bounds or [])
        elif spec.kind == "bool":
            values = [False, True]
        else:
            lo, hi = spec.bounds
            values = [lo, hi]
        axes.append([(spec.name, v) for v in values])
    return [dict(combo) for combo in itertools.product(*axes)]


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_every_tunable_corner_constructs(name):
    cls = strategy_catalog()[name]
    failures = []
    for corner in _corners(cls):
        try:
            cls(corner)
        except Exception as exc:
            failures.append((corner, f"{type(exc).__name__}: {exc}"))
    assert not failures, failures[:3]


def test_regime_filter_clamps_k_to_its_conditions():
    from stonks.strategies.regime import RegimeFilter

    f = RegimeFilter({"k": 10})
    assert int(f.params["k"]) == len(f.conditions)


def test_ma_crossover_fast_bounds_stay_below_slow_bounds():
    from stonks.strategies.examples.ma_crossover import MACrossoverStrategy

    spec = {s.name: s for s in MACrossoverStrategy.parameter_spec()}
    assert spec["fast"].bounds[1] < spec["slow"].bounds[0]
