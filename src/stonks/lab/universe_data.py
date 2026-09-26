"""Get a lab dataset's universe and data ready before the preflight.

A :class:`~stonks.lab.dataset.LabDataset` with a ``universe_id`` and no
tickers gets the members of that universe on any day of its window,
delisted names included (principle P14). An explicit ticker list is kept;
the preflight then reports members missing from it.

With a :class:`~stonks.ingest.ensure.DataEnsurer` (opt in), the missing
bars of those tickers (and of the reference and benchmark tickers) are fetched for the
window plus the strategy's warm-up. The preflight then judges the data
the run will really read.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

from stonks.backtest.benchmark import normalize_spec
from stonks.lab.dataset import LabDataset, embargo_calendar_days
from stonks.lab.universe import resolve_window
from stonks.logging import get_logger
from stonks.strategies.base import strategy_data_tickers

_log = get_logger("stonks.lab.universe_data")


def prepare_dataset(
    dataset: LabDataset, *, ensurer: Any = None, strategy: Any = None
) -> tuple[LabDataset, Any]:
    """The dataset with its universe resolved, and the ensure report
    (``None`` without an ensurer). An unknown universe id is left for the
    preflight to report."""
    universe_id = getattr(dataset, "universe_id", None)
    if universe_id and not dataset.universe and dataset.lake is not None:
        try:
            members = resolve_window(dataset.lake, universe_id, dataset.start, dataset.end)
        except KeyError as exc:
            _log.warning("lab.universe.unknown", universe_id=universe_id, error=str(exc))
        else:
            dataset = dataclasses.replace(dataset, universe=members)
            _log.info("lab.universe.resolved", universe_id=universe_id, members=len(members))
    if ensurer is None or not dataset.universe:
        return dataset, None
    # reference tickers are read but never traded: they need data too
    tickers = [*dataset.universe, *dataset.reference_tickers, *strategy_data_tickers(strategy)]
    spec = normalize_spec(getattr(dataset, "benchmark", None))
    if spec is not None and spec not in ("auto", "ew"):
        tickers.append(spec)
    warmup = int(getattr(strategy, "required_history_bars", 0) or 0)
    start = dataset.start - timedelta(days=embargo_calendar_days(warmup, dataset.interval))
    report = ensurer.ensure(list(dict.fromkeys(tickers)), start, dataset.end, dataset.interval)
    return dataset, report
