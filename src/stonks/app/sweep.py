"""Lab sweep: run many catalogued strategies through the lab on a ticker
basket and summarise the verdicts (``stonks lab sweep``).

Planning: a strategy with a ``ticker`` parameter trades one symbol, so it
gets one lab run per basket ticker (the ticker pinned for the search, like
``--params '{"ticker": ...}'``); any other strategy is universe-aware and
gets one run over the whole basket. Wrapper strategies (they need an inner
strategy) only run when named explicitly.

Execution: every (strategy, ticker) run is one task of the one process
pool, ``lab.parallel.run_tasks``. The basket's bars are copied once into a
read-only :class:`~stonks.lab.parallel.LakeSnapshot` that every worker
opens; each worker keeps its own state-store connection so every run and
trial still lands in the trial ledger. Each task goes through the same
:func:`~stonks.app.lab.execute_lab_run` as ``stonks lab run`` (its tuner
runs in-process inside the worker: pools never nest). Rows come back in
plan order and do not depend on the worker count; a task that raises
becomes an ``error`` row. Sweeps never register strategies.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from stonks.app.lab import LabRunOptions, LabRunRequest, execute_lab_run
from stonks.app.strategies import StrategyRef
from stonks.lab.catalog import is_wrapper, load_strategy_class, resolve_strategy, strategy_catalog
from stonks.lab.dataset import data_tickers
from stonks.lab.parallel import LakeSnapshot, ParallelSettings, planned_workers, run_tasks
from stonks.logging import get_logger
from stonks.production.universe import window_tickers
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.base import strategy_data_tickers
from stonks.universes.base import UNIVERSE_ID_PATTERN

_log = get_logger("stonks.app.sweep")


@dataclass(frozen=True)
class SweepTask:
    """One lab run of a sweep: ``ticker`` is None for a basket-wide run."""

    strategy_id: str
    class_path: str
    ticker: str | None


@dataclass
class SweepRow:
    strategy: str
    ticker: str | None
    verdict: str  # "pass" | "fail" | "error"
    best_score: float | None = None
    best_params: dict[str, Any] = field(default_factory=dict)
    n_trials: int = 0
    run_id: str = ""
    #: ``test_id -> {"passed": bool, "metrics": {...}}``.
    survival: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


def plan_sweep(
    tickers: Sequence[str],
    strategies: Sequence[str] | None = None,
    exclude: Sequence[str] = (),
) -> list[SweepTask]:
    """The sweep's tasks in a stable order: strategies by id, tickers in
    basket order. ``strategies`` (ids, class names or ``module:Class``)
    default to every catalogued non-wrapper strategy."""
    if strategies:
        classes = [resolve_strategy(name) for name in strategies]
    else:
        classes = [cls for cls in strategy_catalog().values() if not is_wrapper(cls)]
    skipped = {resolve_strategy(name).id for name in exclude}
    tasks: list[SweepTask] = []
    for cls in sorted({c.id: c for c in classes}.values(), key=lambda c: c.id):
        if cls.id in skipped:
            continue
        class_path = f"{cls.__module__}:{cls.__name__}"
        if any(spec.name == "ticker" for spec in cls.parameter_spec()):
            tasks.extend(SweepTask(cls.id, class_path, t) for t in tickers)
        else:
            tasks.append(SweepTask(cls.id, class_path, None))
    return tasks


@dataclass(frozen=True)
class _Payload:
    settings: Any
    request: LabRunRequest
    snapshot_path: Path


@dataclass
class _WorkerState:
    payload: _Payload
    lake: DuckDBLake
    state: SqliteState


def _open_worker(payload: _Payload) -> _WorkerState:
    """Once per worker: a read-only connection to the basket snapshot and
    a state-store connection for the trial ledger (kept for the worker's
    life)."""
    lake = DuckDBLake(payload.snapshot_path, read_only=True)
    lake.con.execute("SET threads = 1")
    return _WorkerState(payload, lake, SqliteState(payload.settings.state.path))


def _run_task(worker: _WorkerState, task: SweepTask) -> SweepRow:
    payload = worker.payload
    universe = [task.ticker] if task.ticker is not None else list(payload.request.universe)
    request = payload.request.model_copy(
        update={"strategy": StrategyRef(class_path=task.class_path), "universe": universe}
    )
    try:
        cls = load_strategy_class(task.class_path)
        pinned = {"ticker": task.ticker} if task.ticker is not None else None
        result = execute_lab_run(
            payload.settings,
            cls,
            request,
            lake=worker.lake,
            state=worker.state,
            fixed_params=pinned,
            # pools never nest: the tuner runs in this worker
            parallel=ParallelSettings(max_workers=1),
        ).result
    except Exception as exc:  # one broken strategy must not end the sweep
        _log.warning(
            "sweep.task.failed", strategy=task.strategy_id, ticker=task.ticker, error=str(exc)
        )
        return SweepRow(
            strategy=task.strategy_id,
            ticker=task.ticker,
            verdict="error",
            error=f"{type(exc).__name__}: {exc}",
        )
    return SweepRow(
        strategy=task.strategy_id,
        ticker=task.ticker,
        verdict=result.verdict,
        best_score=_finite(result.best_score),
        best_params=dict(result.best_params),
        n_trials=result.n_trials_run,
        run_id=result.run_id,
        survival={
            r.test_id: {
                "passed": bool(r.passed),
                "metrics": {k: _finite(v) for k, v in dict(r.metrics).items()},
            }
            for r in result.survival_reports
        },
    )


def snapshot_tickers(
    request: LabRunRequest, tasks: Sequence[SweepTask], *, default_benchmark: str = "auto"
) -> list[str]:
    """Every ticker a sweep reads (RS-01), through the lab's shared
    :func:`~stonks.lab.dataset.data_tickers`: the basket, each strategy's
    ``data_tickers()`` (reference tickers it reads but never trades, from
    default params) and the benchmark ticker (``request.benchmark``, else
    ``default_benchmark``)."""
    extra: list[str] = []
    for task in tasks:
        try:
            strategy = resolve_strategy(task.class_path)({})
        except Exception:  # an unbuildable strategy fails in its own task
            continue
        extra.extend(strategy_data_tickers(strategy))
    context = request.model_copy(update={"benchmark": request.benchmark or default_benchmark})
    return data_tickers(context, extra)


def run_sweep(
    settings: Any,
    tasks: Sequence[SweepTask],
    request: LabRunRequest,
    *,
    lake: DuckDBLake,
    parallel: ParallelSettings | None = None,
) -> list[SweepRow]:
    """Run every task; ``request`` carries the shared lab options and the
    basket (``universe``). Rows come back in task order."""
    if request.registers:
        raise ValueError("a sweep never registers strategies")
    if not tasks:
        return []
    with SqliteState(settings.state.path) as state:
        state.migrate()
    parallel = parallel or settings.lab.parallel
    with LakeSnapshot.build(
        lake,
        snapshot_tickers(request, tasks, default_benchmark=settings.lab.benchmark),
        end=request.end,
    ) as snapshot:
        payload = _Payload(settings, request, snapshot.path)
        if planned_workers(len(tasks), settings=parallel) > 1:
            return run_tasks(
                _run_task,
                list(tasks),
                setup=_open_worker,
                payload=payload,
                settings=parallel,
                root_seed=request.seed,
            )
        # In-process: same task function, but close the connections before
        # the snapshot is deleted (Windows cannot delete an open file).
        worker = _open_worker(payload)
        try:
            return run_tasks(
                _run_task, list(tasks), payload=worker, max_workers=1, root_seed=request.seed
            )
        finally:
            worker.lake.close()
            worker.state.close()


# ---- output ---------------------------------------------------------------------


def row_record(row: SweepRow) -> dict[str, Any]:
    """A flat CSV record: fixed columns, then ``<test>.passed`` and
    ``<test>.<metric>`` for every survival test."""
    record: dict[str, Any] = {
        "strategy": row.strategy,
        "ticker": row.ticker or "*",
        "verdict": row.verdict,
        "best_score": row.best_score,
        "best_params": json.dumps(row.best_params, sort_keys=True, default=str),
        "n_trials": row.n_trials,
        "run_id": row.run_id,
        "error": row.error or "",
    }
    for test_id, report in row.survival.items():
        record[f"{test_id}.passed"] = report["passed"]
        for name, value in sorted(report["metrics"].items()):
            record[f"{test_id}.{name}"] = value
    return record


def write_csv(rows: Iterable[SweepRow], path: Path) -> None:
    records = [row_record(r) for r in rows]
    fixed = list(row_record(SweepRow(strategy="", ticker=None, verdict="")))
    extra = sorted({k for rec in records for k in rec} - set(fixed))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[*fixed, *extra])
        writer.writeheader()
        writer.writerows(records)


def write_json(rows: Iterable[SweepRow], path: Path) -> None:
    doc = [asdict(r) for r in rows]
    path.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


# ---- API: request and result views -----------------------------------------------------


class SweepRequest(LabRunOptions):
    """A sweep over a basket: ``universe`` (tickers) or ``universe_id`` (every
    member during the window). ``strategies`` default to every catalogued
    non-wrapper strategy. The lab options apply to every run; sweeps never
    register strategies."""

    universe: list[str] = Field(default_factory=list)
    universe_id: str | None = Field(default=None, pattern=UNIVERSE_ID_PATTERN)
    start: date
    end: date
    interval: str = "1d"
    #: Strategy ids, class names or ``module:Class`` (default: all).
    strategies: list[str] | None = Field(default=None, max_length=200)
    exclude: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if not self.universe and not self.universe_id:
            raise ValueError("give universe (tickers) or universe_id")
        if self.registers:
            raise ValueError("a sweep never registers strategies")
        plan_sweep(["X"], self.strategies, self.exclude)  # unknown names fail here
        return self


class SweepRowView(BaseModel):
    strategy: str
    #: ``None`` for a basket-wide run.
    ticker: str | None
    verdict: Literal["pass", "fail", "error"]
    best_score: float | None = None
    best_params: dict[str, Any] = Field(default_factory=dict)
    n_trials: int = 0
    run_id: str = ""
    #: ``test_id -> {"passed": bool, "metrics": {...}}``.
    survival: dict[str, dict[str, Any]] = Field(default_factory=dict)
    error: str | None = None


class SweepResultView(BaseModel):
    universe: list[str]
    universe_id: str | None = None
    rows: list[SweepRowView]
    passed: int
    failed: int
    errors: int


def execute_sweep(
    settings: Any,
    request: SweepRequest,
    *,
    lake: DuckDBLake,
    parallel: ParallelSettings | None = None,
) -> SweepResultView:
    """Plan and run ``request`` on ``lake`` (the API's sweep job)."""
    basket = list(request.universe) or window_tickers(
        lake, request.universe_id or [], request.start, request.end
    )
    tasks = plan_sweep(basket, request.strategies, request.exclude)
    if not tasks:
        raise ValueError("no strategies left to sweep")
    options = request.model_dump(exclude={"strategies", "exclude", "universe", "universe_id"})
    lab_request = LabRunRequest(
        **options,
        strategy=StrategyRef(class_path=tasks[0].class_path),
        universe=basket,
        universe_id=request.universe_id,
    )
    rows = run_sweep(settings, tasks, lab_request, lake=lake, parallel=parallel)
    return SweepResultView(
        universe=basket,
        universe_id=request.universe_id,
        rows=[SweepRowView(**asdict(r)) for r in rows],
        passed=sum(r.verdict == "pass" for r in rows),
        failed=sum(r.verdict == "fail" for r in rows),
        errors=sum(r.verdict == "error" for r in rows),
    )
