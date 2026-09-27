"""Many independent backtests of one strategy, fanned out over
``lab.parallel.run_tasks``: the re-runs behind the cost-stress (BL-18),
plateau and cross-instrument (BL-19) tests.

A :class:`Rerun` names one variation of the dataset's validation (or
train / full) backtest: other params (rebuilt and re-fitted), another
universe, other costs, and optionally the run's objective score on the
train window. :func:`run_reruns` returns one :class:`RerunResult` per
rerun, in order.

Determinism: every rerun starts from the same strategy state (params
reruns build and fit a fresh instance; the others reload the shipped
strategy from one ``save`` taken up front, the registry's round-trip
contract, on both the serial and the pool path), results come back in
task order, and no rerun draws random numbers, so the output does not
depend on ``max_workers``. On the pool path each worker opens its own
read-only snapshot of the lake (``lab.parallel.dataset_snapshot``) over
the union of every rerun's universe. Work that cannot be pickled (a local
strategy class, an objective holding a live handle) runs in-process
instead.
"""

from __future__ import annotations

import dataclasses
import math
import pickle
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from stonks.core.protocols import Objective, Strategy
from stonks.lab.backtesting import run_backtest
from stonks.lab.parallel import (
    DatasetSpec,
    PortableStrategy,
    dataset_snapshot,
    planned_workers,
    run_tasks,
)
from stonks.lab.tuning.base import evaluate_trial
from stonks.logging import get_logger
from stonks.strategies.base import strategy_data_tickers

_log = get_logger("stonks.lab.survival.reruns")

Window = Literal["train", "val", "full"]


@dataclass(frozen=True)
class Rerun:
    window: Window = "val"
    #: Full params of a rebuilt, re-fitted strategy; ``None`` backtests the
    #: shipped strategy as it is.
    params: Mapping[str, Any] | None = None
    #: Universe override; ``None`` keeps the dataset's.
    universe: tuple[str, ...] | None = None
    #: Cost override (a ``CostModelSettings``, or ``None`` for zero costs);
    #: only applied when ``override_costs``.
    costs: Any = None
    override_costs: bool = False
    #: Shorting override (a ``ShortingSettings``); only applied when
    #: ``override_shorting``.
    shorting: Any = None
    override_shorting: bool = False
    #: Also score the strategy with the run's objective (train window).
    score_objective: bool = False


@dataclass(frozen=True)
class RerunResult:
    sharpe: float = math.nan
    final_return: float = math.nan
    max_drawdown: float = math.nan
    #: Round trips, open lots (marked at the last close) included.
    n_round_trips: int = 0
    #: Closed round trips.
    n_trades: int = 0
    #: Total P&L of every round trip, open lots marked.
    pnl: float = 0.0
    #: Mean P&L per round trip, open lots marked; 0.0 without trades.
    expectancy: float = 0.0
    turnover_annual: float = 0.0
    cost_drag_annual: float = 0.0
    costs_paid: float = 0.0
    #: Borrow fees and debit interest paid (0 for a long-only backtest).
    financing_paid: float = 0.0
    #: The objective's score when asked for (``score_objective``).
    objective_score: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def run_reruns(
    strategy: Strategy,
    dataset: Any,
    reruns: Sequence[Rerun],
    *,
    objective: Objective | None = None,
    max_workers: int | None = None,
    log_prefix: str = "reruns",
) -> list[RerunResult]:
    """One :class:`RerunResult` per rerun, in order (see module doc)."""
    reruns = list(reruns)
    if any(r.score_objective for r in reruns) and objective is None:
        raise ValueError("a rerun asks for the objective score but no objective was given")
    saved = PortableStrategy(strategy).__getstate__()["portable"]
    workers = planned_workers(len(reruns), max_workers=max_workers)
    if workers > 1:
        problem = _unpicklable(_State(saved, objective, _without_lake(dataset)), reruns[0])
        if problem is None:
            universe = _union_universe(dataset, reruns)
            extra = strategy_data_tickers(strategy)
            with dataset_snapshot(_with_universe(dataset, universe), extra) as shipped:
                return run_tasks(
                    _run_one,
                    reruns,
                    setup=_open_state,
                    payload=_State(saved, objective, shipped),
                    max_workers=workers,
                )
        _log.warning(f"{log_prefix}.parallel.unavailable", workers=workers, reason=problem)
    return run_tasks(_run_one, reruns, payload=_State(saved, objective, dataset), max_workers=1)


@dataclass(frozen=True)
class _State:
    #: The strategy's class and saved files (``PortableStrategy`` state).
    saved: tuple[type, dict[str, bytes]]
    objective: Objective | None
    dataset: Any


def _open_state(state: _State) -> _State:
    dataset = state.dataset.open() if isinstance(state.dataset, DatasetSpec) else state.dataset
    return dataclasses.replace(state, dataset=dataset)


def _run_one(state: _State, rerun: Rerun) -> RerunResult:
    try:
        dataset = state.dataset
        if rerun.universe is not None:
            dataset = _with_universe(dataset, list(rerun.universe))
        if rerun.override_costs:
            dataset = dataclasses.replace(dataset, costs=rerun.costs)
        if rerun.override_shorting:
            dataset = dataclasses.replace(dataset, shorting=rerun.shorting)
        objective_score: float | None = None
        if rerun.score_objective:
            assert state.objective is not None
            scored = _strategy_for(state, rerun, dataset)
            objective_score = float(evaluate_trial(state.objective, scored, dataset).score)
        strategy = _strategy_for(state, rerun, dataset)  # fresh state for the backtest
        report = run_backtest(strategy, dataset, _window(dataset, rerun.window))
    except Exception as exc:
        _log.warning("reruns.failed", rerun=repr(rerun), error=str(exc))
        return RerunResult(error=f"{type(exc).__name__}: {exc}")
    trades = report.trades
    pnl = float(sum(t.pnl for t in trades))
    stats = report.trade_stats
    return RerunResult(
        sharpe=float(report.sharpe),
        final_return=float(report.final_return),
        max_drawdown=float(report.max_drawdown),
        n_round_trips=len(trades),
        n_trades=int(stats.n_trades),
        pnl=pnl,
        expectancy=pnl / len(trades) if trades else 0.0,
        turnover_annual=float(stats.turnover_annual),
        cost_drag_annual=float(stats.cost_drag_annual),
        costs_paid=float(stats.costs_paid),
        financing_paid=(
            float(report.short_book.financing_total) if report.short_book is not None else 0.0
        ),
        objective_score=objective_score,
    )


def _strategy_for(state: _State, rerun: Rerun, dataset: Any) -> Strategy:
    cls = state.saved[0]
    if rerun.params is not None:
        strategy = cls(dict(rerun.params))
        strategy.fit(dataset)
        return strategy
    handle = PortableStrategy.__new__(PortableStrategy)
    handle.__setstate__({"portable": state.saved})
    return handle.strategy


def _window(dataset: Any, window: Window) -> Any:
    if window == "train":
        return dataset.train_window
    if window == "full":
        return dataset.full_window
    return dataset.val_window


def _union_universe(dataset: Any, reruns: Sequence[Rerun]) -> list[str]:
    tickers = list(dataset.universe)
    for rerun in reruns:
        for t in rerun.universe or ():
            if t not in tickers:
                tickers.append(t)
    return tickers


def _with_universe(dataset: Any, universe: list[str]) -> Any:
    if list(dataset.universe) == universe:
        return dataset
    return dataclasses.replace(dataset, universe=universe)


def _without_lake(dataset: Any) -> Any:
    if dataclasses.is_dataclass(dataset) and hasattr(dataset, "lake"):
        return dataclasses.replace(dataset, lake=None)
    return dataset


def _unpicklable(*objects: Any) -> str | None:
    try:
        pickle.dumps(objects)
    except Exception as exc:  # pickle raises many types
        return f"{type(exc).__name__}: {exc}"
    return None
