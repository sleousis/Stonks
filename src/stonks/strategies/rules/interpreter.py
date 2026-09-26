"""Evaluate a validated :class:`RuleSpec` against a bar window.

The interpreter walks the typed condition tree node by node; there is no
string parsing and no ``eval``. Each indicator is reduced to a
``(previous, current)`` pair: comparisons read ``current``, the
``crosses_*`` operators read both.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
import pandas as pd

from stonks.features.indicators import true_range
from stonks.strategies.rules.indicators import compute_indicator, required_bars
from stonks.strategies.rules.spec import (
    AllCondition,
    AnyCondition,
    CompareCondition,
    ConstantOperand,
    NotCondition,
    RiskExits,
    RuleSpec,
    referenced_indicators,
)

Values = Mapping[str, tuple[float, float]]


def window_size(spec: RuleSpec) -> int:
    """Bars to fetch per evaluation: the longest warm-up plus one previous
    bar for the ``crosses_*`` operators."""
    need = max(required_bars(ind) for ind in spec.indicators)
    if spec.risk.has_trailing_stop:
        need = max(need, spec.risk.trailing_stop_period + 1)
    return need + 1


def stop_width(risk: RiskExits, bars: pd.DataFrame, bars_per_year: float) -> float | None:
    """Price distance of the trailing stop below the high-water mark on the
    last bar of ``bars``: the tighter of the vol and ATR stops that are
    set, or ``None`` when neither is set or there are too few bars."""
    period = risk.trailing_stop_period
    if not risk.has_trailing_stop or bars is None or len(bars) < period + 1:
        return None
    window = bars.iloc[-(period + 1) :]
    close = window["close"].astype(float).reset_index(drop=True)
    widths: list[float] = []
    if risk.trailing_stop_vol_multiple is not None:
        sigma = float(np.log(close).diff().std(ddof=1))
        if math.isfinite(sigma):
            annual = sigma * math.sqrt(bars_per_year)
            widths.append(risk.trailing_stop_vol_multiple * annual * float(close.iloc[-1]))
    if risk.trailing_stop_atr_multiple is not None:
        high = window["high"].astype(float).reset_index(drop=True)
        low = window["low"].astype(float).reset_index(drop=True)
        tr = true_range(high, low, close).iloc[1:]
        atr_value = float(tr.mean())
        if math.isfinite(atr_value):
            widths.append(risk.trailing_stop_atr_multiple * atr_value)
    return min(widths) if widths else None


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
