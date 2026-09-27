"""A factor dataset (roadmap 22.8): many factors and a next-open label as
one table, the input a model trains on (Qlib's Alpha158 handler, with the
label this system trades on).

Rows are ``(timestamp, ticker)`` where the ticker has a bar and at least one
factor value; columns are the factor ids and, with ``label_horizon``, a
``label`` column holding ``O[t+1+h] / O[t+1] - 1``
(:func:`~stonks.factors.expression.next_open_label`). Bars are read once
for every expression factor, from the longest warm-up before ``start`` to
``end`` and never later, so the label of the last dates is empty (P12).
Other factors (fundamentals) come from their own point-in-time panels.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pandas as pd

from stonks.core.timeutil import day_end, day_start
from stonks.factors.base import ExpressionFactor, Factor
from stonks.factors.engine import (
    PanelRequest,
    evaluate,
    prepare_bars,
    read_bars,
    warmup_days,
)
from stonks.factors.expression import next_open_label
from stonks.factors.panels import FactorEngine
from stonks.store.corporate_actions import LakeCorporateActions

__all__ = ["LABEL", "factor_dataset"]

LABEL = "label"


def factor_dataset(
    factors: Sequence[Factor],
    lake: Any,
    request: PanelRequest,
    *,
    label_horizon: int | None = 1,
    dates: pd.DatetimeIndex | None = None,
    engine: FactorEngine | None = None,
) -> pd.DataFrame:
    """The dataset of ``factors`` over ``request`` (module doc), at every
    bar or at ``dates`` only. ``label_horizon=None`` leaves the label out."""
    if not factors:
        raise ValueError("name at least one factor")
    engine = engine or FactorEngine(lake)
    expressions = [f for f in factors if isinstance(f, ExpressionFactor)]
    warm = max((f.lookback_bars for f in expressions), default=0)
    lo = day_start(request.start - timedelta(days=warmup_days(warm, request.interval)))
    raw = read_bars(lake, request.universe, request.interval, lo, day_end(request.end))
    actions = LakeCorporateActions(lake).load(list(request.universe))
    prepared = prepare_bars(raw, actions, request.membership)
    start = day_start(request.start)
    columns = list(request.universe)

    panels: dict[str, pd.DataFrame] = {}
    for factor in factors:
        if isinstance(factor, ExpressionFactor):
            panels[factor.id] = evaluate(factor.node, prepared, columns, start=start)
        else:
            panels[factor.id] = engine.panel(factor, request, dates=dates)
    if label_horizon is not None:
        panels[LABEL] = evaluate(next_open_label(label_horizon), prepared, columns, start=start)

    stamps = pd.DatetimeIndex(sorted(prepared["timestamp"].unique()))
    stamps = stamps[stamps >= pd.Timestamp(start)]
    if dates is not None:
        stamps = pd.DatetimeIndex(dates)
    long = {
        name: panel.reindex(index=stamps, columns=columns).stack(future_stack=True)
        for name, panel in panels.items()
    }
    out = pd.DataFrame(long)
    out.index = out.index.set_names(["timestamp", "ticker"])
    features = [f.id for f in factors]
    keep = out[features].notna().any(axis=1).to_numpy()
    return pd.DataFrame(out.loc[keep]).astype(float)
