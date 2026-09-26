"""Evaluate a validated :class:`RuleSpec` against a bar window.

The interpreter walks the typed condition tree node by node; there is no
string parsing and no ``eval``. Each indicator is reduced to a
``(previous, current)`` pair: comparisons read ``current``, the
``crosses_*`` operators read both.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import pandas as pd

from stonks.strategies.rules.indicators import compute_indicator, required_bars
from stonks.strategies.rules.spec import (
    AllCondition,
    AnyCondition,
    CompareCondition,
    ConstantOperand,
    NotCondition,
    RuleSpec,
    referenced_indicators,
)

Values = Mapping[str, tuple[float, float]]


def window_size(spec: RuleSpec) -> int:
    """Bars to fetch per evaluation: the longest warm-up plus one previous
    bar for the ``crosses_*`` operators."""
    return max(required_bars(ind) for ind in spec.indicators) + 1


def used_indicators(spec: RuleSpec) -> set[str]:
    return referenced_indicators(spec.entry) | referenced_indicators(spec.exit) | {spec.rank.by}


def snapshot(spec: RuleSpec, bars: pd.DataFrame) -> dict[str, tuple[float, float]] | None:
    """``{indicator id: (previous, current)}`` on the last bar of ``bars``,
    or ``None`` while any indicator the spec uses is still warming up."""
    if bars is None or bars.empty:
        return None
    used = used_indicators(spec)
    values: dict[str, tuple[float, float]] = {}
    for ind in spec.indicators:
        if ind.id not in used:
            continue
        if len(bars) < required_bars(ind):
            return None
        series = compute_indicator(ind, bars)
        current = float(series.iloc[-1])
        if math.isnan(current):
            return None
        previous = float(series.iloc[-2]) if len(series) >= 2 else math.nan
        values[ind.id] = (previous, current)
    return values


def evaluate(cond: object, values: Values) -> bool:
    """Truth of a condition tree; comparisons touching NaN are false."""
    if isinstance(cond, AllCondition):
        return all(evaluate(c, values) for c in cond.conditions)
    if isinstance(cond, AnyCondition):
        return any(evaluate(c, values) for c in cond.conditions)
    if isinstance(cond, NotCondition):
        return not evaluate(cond.condition, values)
    if isinstance(cond, CompareCondition):
        return _compare(cond, values)
    raise TypeError(f"not a condition node: {type(cond).__name__}")


def _operand(operand: object, values: Values) -> tuple[float, float]:
    if isinstance(operand, ConstantOperand):
        return operand.value, operand.value
    return values[operand.id]  # type: ignore[attr-defined]


def _compare(cond: CompareCondition, values: Values) -> bool:
    l_prev, l_cur = _operand(cond.left, values)
    r_prev, r_cur = _operand(cond.right, values)
    if math.isnan(l_cur) or math.isnan(r_cur):
        return False
    op = cond.op
    if op == "<":
        return l_cur < r_cur
    if op == "<=":
        return l_cur <= r_cur
    if op == ">":
        return l_cur > r_cur
    if op == ">=":
        return l_cur >= r_cur
    if math.isnan(l_prev) or math.isnan(r_prev):
        return False
    if op == "crosses_above":
        return l_prev <= r_prev and l_cur > r_cur
    if op == "crosses_below":
        return l_prev >= r_prev and l_cur < r_cur
    raise ValueError(f"unknown operator {op!r}")
