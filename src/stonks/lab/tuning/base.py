"""Tuner helpers shared by concrete implementations.

Every tuner scores its candidates through :func:`evaluate_candidates`. A
lab run sets a run-scoped trial sink (:func:`trial_sink`) around its
search, and :func:`evaluate_candidates` hands the sink each trial as it
finishes, with the trial's seed, so a cancel, a crash or a broken pool
cannot hide trials that already ran (P2). A search inside a trial (an
objective that tunes) is not the run's search and is not reported.

A search where no trial scored has no winner: :func:`best_of` then returns
NaN, and :func:`tune_and_fit` with ``require_score`` raises
:class:`TuningFailed` with the first trial's error.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import pickle
import random
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from stonks.core.params import ParameterSpec, Params, ParamSpace, tunable_only
from stonks.core.protocols import Objective, Strategy, TrialOutcome, TunerResult
from stonks.lab.parallel import (
    DatasetSpec,
    ParallelSettings,
    dataset_snapshot,
    planned_workers,
    run_tasks,
    task_seeds,
)
from stonks.logging import get_logger
from stonks.strategies.base import strategy_data_tickers
from stonks.strategies.costs import bind_costs

if TYPE_CHECKING:  # pragma: no cover
    from stonks.lab.survival.base import TuningSetup

_log = get_logger("stonks.lab.tuning")

_FROM_DATASET: Any = object()

#: Gets finished trials and their seeds (``None`` when a trial ran unseeded).
TrialSink = Callable[[Sequence[TrialOutcome], Sequence[int | None]], None]

_TRIAL_SINK: ContextVar[TrialSink | None] = ContextVar("stonks_lab_trial_sink", default=None)


@contextmanager
def trial_sink(sink: TrialSink | None) -> Iterator[None]:
    """Report every trial :func:`evaluate_candidates` finishes inside the
    block to ``sink`` (``None`` reports nothing). Scoped to the current
    context, so a run in another thread keeps its own sink."""
    token = _TRIAL_SINK.set(sink)
    try:
        yield
    finally:
        _TRIAL_SINK.reset(token)


class TuningFailed(RuntimeError):
    """Every trial of a search failed, so it has no winner. Carries the
    tuner's result (its trials still count, P2) and the first trial error."""

    def __init__(self, tuned: TunerResult, first_error: str | None) -> None:
        self.tuned = tuned
        self.first_error = first_error
        n = len(getattr(tuned, "history", None) or [])
        detail = first_error if first_error else "no trial reported an error"
        super().__init__(f"all {n} tuning trials failed; the first error was: {detail}")


def first_trial_error(tuned: Any) -> str | None:
    """The first error a tuner's trials report, else ``None``."""
    for outcome in getattr(tuned, "trials", None) or []:
        error = getattr(outcome, "error", None)
        if error:
            return str(error)
    return None


def has_score(tuned: Any) -> bool:
    """True when the search found a winner: a finite best score."""
    try:
        return math.isfinite(float(tuned.best_score))
    except (TypeError, ValueError):
        return False


def fitted_strategy(
    strategy_cls: type[Strategy],
    params: Params,
    dataset: Any,
    *,
    costs: Any = _FROM_DATASET,
) -> Strategy:
    """``strategy_cls(params)`` with a cost model bound
    (:mod:`stonks.strategies.costs`: ``costs``, else the dataset's), then
    fitted on ``dataset``. How the lab builds every strategy it runs."""
    strategy = strategy_cls(dict(params))
    bind_costs(strategy, getattr(dataset, "costs", None) if costs is _FROM_DATASET else costs)
    strategy.fit(dataset)
    return strategy


def tune_and_fit(
    strategy_cls: type[Strategy],
    dataset: Any,
    setup: TuningSetup,
    fixed_params: Mapping[str, Any] | None = None,
    *,
    require_score: bool = False,
) -> tuple[Strategy, TunerResult]:
    """Tune ``strategy_cls`` on ``dataset``'s train window, then build and
    fit the winning configuration on the same dataset. The one tune → fit
    sequence shared by the runner and every re-tuning survival test.

    ``fixed_params`` pin parameters for the whole search (see
    :func:`fix_params`); re-tuning tests pass ``fixed_params_of(strategy)``
    so a hand-set non-tunable param (a wrapper's inner strategy, a ticker)
    is never reset to its class default.

    ``require_score`` raises :class:`TuningFailed` before the fit when no
    trial scored. Without it such a search fits the defaults and reports a
    NaN best score (a re-tuning test then scores that fold or draw NaN)."""
    fixed = dict(fixed_params or {})
    space = fix_params(strategy_cls.parameter_spec(), fixed)
    tuned = setup.tuner.tune(
        strategy_cls=strategy_cls,
        param_space=space,
        objective=setup.objective,
        dataset=dataset,
        budget=setup.budget,
    )
    if require_score and not has_score(tuned):
        raise TuningFailed(tuned, first_trial_error(tuned))
    best = {**tuned.best_params, **fixed}
    if best != dict(tuned.best_params):
        tuned = dataclasses.replace(tuned, best_params=best)
    strategy = fitted_strategy(strategy_cls, best, dataset)  # fit: no-op for rule-based
    return strategy, tuned


def fix_params(space: ParamSpace, fixed: Mapping[str, Any]) -> list[ParameterSpec]:
    """``space`` with each ``fixed`` param pinned: its default becomes the
    fixed value and it is no longer tunable, so tuners fill it in through
    their usual default-merging. Unknown names raise ``ValueError``."""
    names = {s.name for s in space}
    unknown = sorted(set(fixed) - names)
    if unknown:
        raise ValueError(f"fixed params not in the parameter spec: {unknown}")
    return [
        dataclasses.replace(s, default=fixed[s.name], tunable=False) if s.name in fixed else s
        for s in space
    ]


def fixed_params_of(strategy: Strategy) -> dict[str, Any]:
    """The non-tunable params of an already-built strategy — what a
    re-tune must carry over instead of taking the class defaults."""
    params = getattr(strategy, "params", None) or {}
    return {
        s.name: params[s.name]
        for s in type(strategy).parameter_spec()
        if not s.tunable and s.name in params
    }


def expand_grid(space: ParamSpace, grid_size: int) -> Iterable[dict[str, Any]]:
    """Yield every combination of parameter values from the tunable part of
    ``space``, discretizing numeric bounds into ``grid_size`` evenly-spaced
    points. Non-tunable params are filled from their defaults by the caller.
    """
    axes = grid_axes(space, grid_size)
    names = [n for n, _ in axes]
    for combo in itertools.product(*(v for _, v in axes)):
        yield dict(zip(names, combo, strict=False))


def grid_axes(space: ParamSpace, grid_size: int) -> list[tuple[str, list[Any]]]:
    """``(name, values)`` per tunable parameter, in spec order."""
    return [(spec.name, _axis_values(spec, grid_size)) for spec in tunable_only(space)]


def grid_size_of(axes: list[tuple[str, list[Any]]]) -> int:
    return math.prod(len(values) for _, values in axes)


def sample_grid(
    axes: list[tuple[str, list[Any]]], k: int, rng: random.Random
) -> list[dict[str, Any]]:
    """Draw ``k`` distinct grid combinations uniformly at random.

    Combinations are addressed by their flat index into the cartesian
    product and decoded mixed-radix, so the full grid is never
    materialized. Returned in grid order for readable trial logs.
    """
    total = grid_size_of(axes)
    indices = sorted(rng.sample(range(total), min(k, total)))
    return [_decode(i, axes) for i in indices]


def _decode(index: int, axes: list[tuple[str, list[Any]]]) -> dict[str, Any]:
    combo: dict[str, Any] = {}
    for name, values in reversed(axes):
        index, pos = divmod(index, len(values))
        combo[name] = values[pos]
    return {name: combo[name] for name, _ in axes}


def _axis_values(spec: ParameterSpec, grid_size: int) -> list[Any]:
    if spec.kind == "categorical":
        return list(spec.bounds or [])
    if spec.kind == "bool":
        return [False, True]
    lo, hi = spec.bounds  # numeric
    if grid_size <= 1:
        return [spec.default]
    step = (hi - lo) / (grid_size - 1)
    points = [lo + step * i for i in range(grid_size)]
    if spec.kind == "int":
        return sorted({int(round(p)) for p in points})
    return points


def merge_with_defaults(params: dict[str, Any], space: ParamSpace) -> dict[str, Any]:
    """Fill any missing params with their ParameterSpec defaults."""
    defaults = {s.name: s.default for s in space}
    return {**defaults, **params}


# ---- trial evaluation -------------------------------------------------------


def evaluate_trial(objective: Objective, strategy: Strategy, dataset: Any) -> TrialOutcome:
    """``objective.evaluate`` when the objective has it, else a summary-only
    outcome around ``objective.score`` (no per-bar returns)."""
    evaluate = getattr(objective, "evaluate", None)
    if evaluate is not None:
        return evaluate(strategy, dataset)
    return TrialOutcome(
        params=dict(getattr(strategy, "params", None) or {}),
        score=objective.score(strategy, dataset),
    )


def best_of(
    outcomes: Sequence[TrialOutcome], direction: str, space: ParamSpace
) -> tuple[dict[str, Any], float]:
    """The first trial with the best score (ties keep the earlier trial;
    NaN never wins, nor does the worst possible value, ``-inf`` when
    maximizing); the defaults with a NaN score when no trial scored, so a
    failed search never passes for a score of 0.0."""
    maximize = direction == "maximize"
    best: TrialOutcome | None = None
    best_score = -math.inf if maximize else math.inf
    for outcome in outcomes:
        if outcome.score > best_score if maximize else outcome.score < best_score:
            best, best_score = outcome, outcome.score
    if best is None:
        return merge_with_defaults({}, space), math.nan
    return dict(best.params), best.score


def evaluate_candidates(
    strategy_cls: type[Strategy],
    candidates: Sequence[Params],
    objective: Objective,
    dataset: Any,
    *,
    parallel: ParallelSettings,
    root_seed: int,
    log_prefix: str = "tuner",
) -> list[TrialOutcome]:
    """Build, fit and score every candidate (full params), one trial per
    candidate, in candidate order, through ``lab.parallel.run_tasks``.

    A trial that raises becomes :meth:`TrialOutcome.failed` (NaN score).
    Each trial runs with the global RNGs seeded from ``root_seed`` and its
    index, so scores never depend on the worker count.

    On the pool path the dataset's lake is replaced by a read-only
    snapshot per worker (``lab.parallel.dataset_snapshot``), and an
    objective exposing ``worker_objective`` (sent to the workers instead
    of itself) and ``checkpoint`` (run in this process between trials,
    e.g. a job-cancellation check) keeps its cancellation working. When
    the work cannot be pickled (a local strategy class, an objective
    holding a live handle) the trials run in-process instead.

    With a :func:`trial_sink` set, each trial goes to the sink as it
    finishes, with its seed. The sink is cleared while the trials run, so a
    search nested inside a trial is never reported as the run's."""
    tasks: list[tuple[int, dict[str, Any]]] = list(enumerate(dict(c) for c in candidates))
    sink = _TRIAL_SINK.get()
    report = None if sink is None else _reporter(sink, task_seeds(root_seed, len(tasks)))
    token = _TRIAL_SINK.set(None)
    try:
        return _evaluate(
            strategy_cls,
            tasks,
            candidates,
            objective,
            dataset,
            parallel,
            root_seed,
            log_prefix,
            report,
        )
    finally:
        _TRIAL_SINK.reset(token)


def _reporter(sink: TrialSink, seeds: list[int]) -> Callable[[int, TrialOutcome], None]:
    """``run_tasks``'s ``on_result``: trial ``i`` goes to ``sink`` with its seed."""

    def report(i: int, outcome: TrialOutcome) -> None:
        sink([outcome], [seeds[i]])

    return report


def _evaluate(
    strategy_cls: type[Strategy],
    tasks: list[tuple[int, dict[str, Any]]],
    candidates: Sequence[Params],
    objective: Objective,
    dataset: Any,
    parallel: ParallelSettings,
    root_seed: int,
    log_prefix: str,
    on_result: Callable[[int, TrialOutcome], None] | None,
) -> list[TrialOutcome]:
    workers = planned_workers(len(tasks), settings=parallel)
    if workers > 1:
        worker_objective = getattr(objective, "worker_objective", objective)
        checkpoint = getattr(objective, "checkpoint", None)
        probe = _TrialState(strategy_cls, worker_objective, _without_lake(dataset), log_prefix)
        problem = _unpicklable(probe, tasks[0])
        if problem is None:
            extra = _candidate_data_tickers(strategy_cls, candidates)
            with dataset_snapshot(dataset, extra) as shipped:
                return run_tasks(
                    _run_trial,
                    tasks,
                    setup=_open_trial_state,
                    payload=_TrialState(strategy_cls, worker_objective, shipped, log_prefix),
                    settings=parallel,
                    root_seed=root_seed,
                    checkpoint=checkpoint,
                    on_result=on_result,
                )
        _log.warning(f"{log_prefix}.parallel.unavailable", workers=workers, reason=problem)
    return run_tasks(
        _run_trial,
        tasks,
        payload=_TrialState(strategy_cls, objective, dataset, log_prefix),
        max_workers=1,
        root_seed=root_seed,
        on_result=on_result,
    )


@dataclasses.dataclass(frozen=True)
class _TrialState:
    strategy_cls: type[Strategy]
    objective: Objective
    dataset: Any
    log_prefix: str


def _open_trial_state(state: _TrialState) -> _TrialState:
    """Worker setup: reopen the dataset on its snapshot (one read-only
    connection per worker, kept for the worker's life)."""
    if isinstance(state.dataset, DatasetSpec):
        return dataclasses.replace(state, dataset=state.dataset.open())
    return state


def _run_trial(state: _TrialState, task: tuple[int, Params]) -> TrialOutcome:
    index, params = task
    try:
        strategy = fitted_strategy(state.strategy_cls, params, state.dataset)  # as LabRunner
        outcome = evaluate_trial(state.objective, strategy, state.dataset)
    except Exception as exc:
        _log.warning(f"{state.log_prefix}.trial.failed", trial=index, params=params, error=str(exc))
        return TrialOutcome.failed(params, str(exc))
    return outcome.with_params(params)


def _candidate_data_tickers(
    strategy_cls: type[Strategy], candidates: Sequence[Params]
) -> list[str]:
    """Every ticker any candidate reads but does not trade (RS-01), so the
    worker snapshot holds what the serial run would read. A candidate that
    cannot be built is skipped here: its trial fails on its own."""
    out: dict[str, None] = {}
    for params in candidates:
        try:
            out.update(dict.fromkeys(strategy_data_tickers(strategy_cls(dict(params)))))
        except Exception:  # the trial itself reports the failure
            continue
    return list(out)


def _without_lake(dataset: Any) -> Any:
    if dataclasses.is_dataclass(dataset) and hasattr(dataset, "lake"):
        return dataclasses.replace(dataset, lake=None)
    return dataset


def _unpicklable(*objects: Any) -> str | None:
    try:
        pickle.dumps(objects)
    except Exception as exc:  # pickle raises many types (PicklingError, TypeError, ...)
        return f"{type(exc).__name__}: {exc}"
    return None
