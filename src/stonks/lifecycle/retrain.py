"""Scheduled retraining (roadmap 22.6).

:func:`retrain_models` refits every retrainable strategy (``retrainable``
on the strategy, see ``strategies.base``) of the configured statuses on
the trailing ``lookback_days`` up to ``as_of``, and records each fit as a
*candidate* version (``registry.versions``). Nothing here changes what
trades: the tick runs the candidate as a model book beside the live
version, and a swap goes through governance later.

- The params never change: a retrain refits the model the lab validated
  on newer data. A new param set is a new lab run and a new strategy id.
- Fits fan out through the one lab pool (``lab.parallel.run_tasks``), on a
  read-only lake snapshot when more than one worker runs.
- The universe and interval come from the lab run's manifest in the
  artifact (``manifest.dataset``), else from the caller's universe. A lab
  run on a stored universe (``universe_id``) is refit on that universe's
  members over the new window, not on the tickers frozen in the manifest.
- A version's ``train_end`` is the last day the fit saw: the day before
  ``as_of``.
- A fit that raises is kept as a ``failed`` version with its error; the
  other strategies still refit.
- A strategy whose newest fit ended within ``min_days_between_fits`` days
  is skipped unless ``force`` is set, so a re-run on the same day does
  nothing.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import (
    DatasetSpec,
    ParallelSettings,
    dataset_snapshot,
    planned_workers,
    run_tasks,
)
from stonks.lifecycle.settings import ModelLifecycleSettings
from stonks.logging import get_logger
from stonks.registry.artifact import ArtifactBundle
from stonks.registry.store import StrategyHandle, StrategyRegistry, _finite
from stonks.registry.versions import ModelVersionRegistry
from stonks.store.state import SqliteState
from stonks.strategies._wrapping import import_strategy_class
from stonks.strategies.base import strategy_data_tickers
from stonks.strategies.costs import bind_costs

if TYPE_CHECKING:
    from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lifecycle.retrain")

RetrainStatus = Literal["candidate", "failed", "skipped"]


def is_retrainable(strategy: Any) -> bool:
    """Whether scheduled retraining applies to ``strategy``."""
    return bool(getattr(strategy, "retrainable", False))


@dataclass(frozen=True)
class RetrainOutcome:
    strategy_id: str
    status: RetrainStatus
    version: int | None = None
    #: Why it was skipped, or the fit's error.
    detail: str | None = None
    train_start: date | None = None
    train_end: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "status": self.status,
            "version": self.version,
            "detail": self.detail,
            "train_start": None if self.train_start is None else self.train_start.isoformat(),
            "train_end": None if self.train_end is None else self.train_end.isoformat(),
        }


@dataclass(frozen=True)
class RetrainSummary:
    as_of: date
    outcomes: list[RetrainOutcome] = field(default_factory=list)

    def count(self, status: RetrainStatus) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "candidates": self.count("candidate"),
            "failed": self.count("failed"),
            "skipped": self.count("skipped"),
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


@dataclass(frozen=True)
class _FitTask:
    strategy_id: str
    class_path: str
    params: dict[str, Any]
    universe: tuple[str, ...]
    interval: str
    start: date
    end: date
    out_dir: str
    version: int
    universe_id: str | None = None

    @property
    def train_end(self) -> date:
        """The fit's last day: the day before ``end`` (``as_of``)."""
        return self.end - timedelta(days=1)


def retrain_models(
    state: SqliteState,
    lake: DuckDBLake,
    registry: StrategyRegistry,
    settings: ModelLifecycleSettings,
    *,
    as_of: date,
    universe: Sequence[str] = (),
    strategy_ids: Sequence[str] | None = None,
    force: bool = False,
    actor: str,
    parallel: ParallelSettings | None = None,
    max_workers: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> RetrainSummary:
    """Refit and record a candidate per retrainable strategy (module doc).
    ``strategy_ids`` names strategies explicitly (any status but retired);
    a named one that cannot retrain is reported as skipped. ``KeyError``
    for an unknown id."""
    versions = ModelVersionRegistry.on(registry)
    outcomes: list[RetrainOutcome] = []
    tasks: list[_FitTask] = []
    start = as_of - timedelta(days=settings.lookback_days)
    for handle, named in _targets(registry, settings, strategy_ids):
        skip = _skip_reason(handle, versions, settings, as_of, force=force, named=named)
        if skip == "":
            continue  # not retrainable and not asked for by name
        if skip is not None:
            outcomes.append(RetrainOutcome(handle.id, "skipped", detail=skip))
            continue
        version, _rel, folder = versions.next_version(handle.id)
        universe_, interval, universe_id = _data_of(handle, universe)
        if universe_id is not None:
            members = _members(lake, universe_id, start, as_of)
            if members is None:
                outcomes.append(
                    RetrainOutcome(
                        handle.id, "skipped", detail=f"stored universe {universe_id!r} is unknown"
                    )
                )
                continue
            universe_ = members
        if not universe_:
            outcomes.append(RetrainOutcome(handle.id, "skipped", detail="no universe to fit on"))
            continue
        tasks.append(
            _FitTask(
                strategy_id=handle.id,
                class_path=handle.class_path,
                params=dict(handle.params),
                universe=tuple(universe_),
                interval=interval,
                start=start,
                end=as_of,
                out_dir=str(folder),
                version=version,
                universe_id=universe_id,
            )
        )
    if tasks:
        results = _run_fits(lake, tasks, parallel, max_workers, checkpoint)
        for task, result in zip(tasks, results, strict=True):
            outcomes.append(_record(versions, task, result, actor))
    order = {sid: i for i, sid in enumerate(strategy_ids or [])}
    outcomes.sort(key=lambda o: order.get(o.strategy_id, len(order)))
    summary = RetrainSummary(as_of=as_of, outcomes=outcomes)
    _log.info(
        "lifecycle.retrain.done",
        as_of=as_of.isoformat(),
        candidates=summary.count("candidate"),
        failed=summary.count("failed"),
        skipped=summary.count("skipped"),
    )
    return summary


# ---- targets ---------------------------------------------------------------------


def _targets(
    registry: StrategyRegistry,
    settings: ModelLifecycleSettings,
    strategy_ids: Sequence[str] | None,
) -> list[tuple[StrategyHandle, bool]]:
    handles = {h.id: h for h in registry.list_all()}
    if strategy_ids:
        missing = [sid for sid in strategy_ids if sid not in handles]
        if missing:
            raise KeyError(missing[0])
        return [(handles[sid], True) for sid in dict.fromkeys(strategy_ids)]
    wanted = set(settings.statuses)
    return [(h, False) for h in handles.values() if h.status in wanted]


def _skip_reason(
    handle: StrategyHandle,
    versions: ModelVersionRegistry,
    settings: ModelLifecycleSettings,
    as_of: date,
    *,
    force: bool,
    named: bool,
) -> str | None:
    """``None`` to fit, ``""`` to leave out silently, else why it is skipped."""
    if handle.status == "retired":
        return "strategy is retired"
    try:
        probe = import_strategy_class(handle.class_path)(dict(handle.params))
    except Exception as exc:
        return f"cannot build the strategy: {type(exc).__name__}: {exc}"
    if not is_retrainable(probe):
        return "strategy does not learn from data" if named else ""
    if force:
        return None
    ends = [v.train_end for v in versions.list(handle.id) if v.status != "failed" and v.train_end]
    # train_end is the day before the fit's as_of: count days between fits
    if ends and (as_of - max(ends)).days - 1 < settings.min_days_between_fits:
        return f"fitted up to {max(ends).isoformat()} already"
    return None


def _data_of(handle: StrategyHandle, fallback: Sequence[str]) -> tuple[list[str], str, str | None]:
    """The lab run's universe, interval and stored universe id from the
    artifact manifest, else ``fallback`` at the daily interval."""
    meta = _read_json(handle.artifact_path / "meta.json")
    dataset = (meta.get("manifest") or {}).get("dataset") or {}
    universe = [str(t) for t in dataset.get("universe") or []]
    interval = str(dataset.get("interval") or Interval.DAY_1.code)
    raw_id = dataset.get("universe_id")
    universe_id = raw_id if isinstance(raw_id, str) and raw_id else None
    return (universe or list(fallback)), interval, universe_id


def _members(lake: DuckDBLake, universe_id: str, start: date, end: date) -> list[str] | None:
    """Members of the stored universe on any day of ``[start, end]`` (P14:
    names that left or died inside the window stay in), or ``None`` when
    the universe is unknown."""
    from stonks.lab.universe import resolve_window

    try:
        return resolve_window(lake, universe_id, start, end)
    except KeyError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# ---- fitting ---------------------------------------------------------------------


def _run_fits(
    lake: DuckDBLake,
    tasks: list[_FitTask],
    parallel: ParallelSettings | None,
    max_workers: int | None,
    checkpoint: Callable[[], None] | None,
) -> list[dict[str, Any]]:
    universe = sorted({t for task in tasks for t in task.universe})
    base = LabDataset(
        lake=lake,
        universe=universe,
        start=min(t.start for t in tasks),
        end=max(t.end for t in tasks),
    )
    workers = planned_workers(len(tasks), max_workers=max_workers, settings=parallel)
    if workers <= 1:
        return run_tasks(_fit_one, tasks, payload=base, max_workers=1, checkpoint=checkpoint)
    extra = sorted({t for task in tasks for t in _task_references(task)})
    with dataset_snapshot(base, extra) as shipped:
        return run_tasks(
            _fit_one,
            tasks,
            setup=_open_dataset,
            payload=shipped,
            max_workers=workers,
            settings=parallel,
            checkpoint=checkpoint,
        )


def _task_references(task: _FitTask) -> tuple[str, ...]:
    try:
        return strategy_data_tickers(import_strategy_class(task.class_path)(dict(task.params)))
    except Exception:
        return ()


def _open_dataset(payload: Any) -> Any:
    return payload.open() if isinstance(payload, DatasetSpec) else payload


def _fit_one(base: LabDataset, task: _FitTask) -> dict[str, Any]:
    """Fit one strategy and save it as a bundle in ``task.out_dir``. Never
    raises: a failure comes back as ``{"ok": False, "error": ...}``."""
    out = Path(task.out_dir)
    try:
        strategy = import_strategy_class(task.class_path)(dict(task.params))
        dataset = dataclasses.replace(
            base,
            universe=list(task.universe),
            interval=Interval.parse(task.interval),
            start=task.start,
            end=task.end,
            train_end=task.train_end,
            universe_id=task.universe_id,
            reference_tickers=(),
        ).with_references(strategy_data_tickers(strategy))
        bind_costs(strategy, getattr(dataset, "costs", None))
        strategy.fit(dataset)
        out.mkdir(parents=True, exist_ok=True)
        strategy.save(out)
        ArtifactBundle(
            path=out,
            class_path=task.class_path,
            params=dict(getattr(strategy, "params", task.params)),
            meta={
                "model_version": task.version,
                "train_window": [task.start.isoformat(), task.train_end.isoformat()],
                "universe": list(task.universe),
                "universe_id": task.universe_id,
                "interval": task.interval,
            },
        ).save()
        return {"ok": True, "fit": _fit_summary(strategy)}
    except Exception as exc:
        shutil.rmtree(out, ignore_errors=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _fit_summary(strategy: Any) -> dict[str, Any]:
    """The strategy's ``fitted_state`` (method or attribute) as plain JSON."""
    state = getattr(strategy, "fitted_state", None)
    try:
        value = state() if callable(state) else state
    except Exception:
        return {}
    if not isinstance(value, Mapping):
        return {}
    return json.loads(json.dumps(_finite(dict(value)), default=str))


def _record(
    versions: ModelVersionRegistry, task: _FitTask, result: Mapping[str, Any], actor: str
) -> RetrainOutcome:
    rel = f"{task.strategy_id}/versions/v{task.version}"
    log = _log.bind(strategy_id=task.strategy_id, version=task.version)
    if result.get("ok"):
        versions.add_candidate(
            task.strategy_id,
            task.version,
            rel,
            train_start=task.start,
            train_end=task.train_end,
            fit=result.get("fit") or {},
            actor=actor,
        )
        log.info("lifecycle.retrain.candidate")
        status: RetrainStatus = "candidate"
        detail = None
    else:
        detail = str(result.get("error") or "fit failed")
        versions.record_failure(
            task.strategy_id,
            task.version,
            rel,
            train_start=task.start,
            train_end=task.train_end,
            error=detail,
            actor=actor,
        )
        log.warning("lifecycle.retrain.failed", error=detail)
        status = "failed"
    return RetrainOutcome(
        task.strategy_id,
        status,
        version=task.version,
        detail=detail,
        train_start=task.start,
        train_end=task.train_end,
    )
