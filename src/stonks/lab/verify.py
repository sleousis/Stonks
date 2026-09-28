"""Rerun a lab result from its stored manifest (roadmap 23.9, P2 and P12).

``stonks lab verify <run or strategy>`` answers: would the same search give
the same answer on today's data? It takes the stored manifest of a lab run
(or of the lab run behind a registered strategy) and:

1. recomputes the data fingerprint over exactly the tickers, window and
   interval the run read, and lists the tickers whose bars, corporate
   actions or point-in-time statement versions changed since (a vendor
   restatement, a new split, a filled gap), with the restated statements
   named apart;
2. compares the config hash and the code version with today's;
3. rebuilds the chosen parameters on the run's dataset and scores them with
   the run's objective on its train window, seeded like the original
   trial, and compares with the stored score.

The result has *moved* when the score changed by more than ``tolerance``.
A changed fingerprint alone is reported, not judged: a restated bar that
does not change the score is harmless. The weekly ``lab_verify`` job runs
this for every active strategy and alerts on the ones that moved.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from stonks.lab.manifest import (
    config_hash,
    fingerprint_changes,
    git_info,
    refingerprint,
    restated_tickers,
)
from stonks.lab.trials import TrialLedger
from stonks.logging import get_logger

_log = get_logger("stonks.lab.verify")

__all__ = ["VerifyReport", "VerifyTarget", "resolve_target", "verify"]


@dataclass(frozen=True)
class VerifyTarget:
    """What to rerun: a lab run's chosen trial, or a registered strategy."""

    target: str
    kind: str  # "run" | "strategy"
    class_path: str
    params: dict[str, Any]
    manifest: dict[str, Any]
    run_id: str | None = None
    objective: str | None = None
    stored_score: float | None = None
    seed: int | None = None
    trial_index: int | None = None


@dataclass(frozen=True)
class VerifyReport:
    target: str
    kind: str
    class_path: str
    run_id: str | None
    objective: str | None
    tolerance: float
    stored_score: float | None = None
    current_score: float | None = None
    data_changed: bool | None = None
    changed_tickers: list[str] = field(default_factory=list)
    #: Tickers whose statement versions changed (a subset of the above).
    restated_tickers: list[str] = field(default_factory=list)
    config_changed: bool | None = None
    code_changed: bool | None = None
    error: str | None = None

    @property
    def score_delta(self) -> float | None:
        if self.stored_score is None or self.current_score is None:
            return None
        if not (math.isfinite(self.stored_score) and math.isfinite(self.current_score)):
            return None
        return self.current_score - self.stored_score

    @property
    def moved(self) -> bool:
        """The score changed beyond the tolerance, or it could not be
        reproduced at all while the stored run had one."""
        delta = self.score_delta
        if delta is not None:
            return abs(delta) > self.tolerance
        return self.stored_score is not None and self.current_score is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "kind": self.kind,
            "class_path": self.class_path,
            "run_id": self.run_id,
            "objective": self.objective,
            "tolerance": self.tolerance,
            "stored_score": self.stored_score,
            "current_score": self.current_score,
            "score_delta": self.score_delta,
            "moved": self.moved,
            "data_changed": self.data_changed,
            "changed_tickers": list(self.changed_tickers),
            "restated_tickers": list(self.restated_tickers),
            "config_changed": self.config_changed,
            "code_changed": self.code_changed,
            "error": self.error,
        }


def resolve_target(state: Any, registry: Any, artifacts_dir: Path, target: str) -> VerifyTarget:
    """A lab run id, else a registered strategy id. ``KeyError`` for neither."""
    ledger = TrialLedger(state, artifacts_dir)
    run = ledger.run(target)
    if run is not None:
        return _run_target(ledger, run, target)
    handle = registry._get_handle(target)  # KeyError when unknown
    from stonks.registry.artifact import ArtifactBundle

    meta = ArtifactBundle.load(handle.artifact_path).meta
    run_id = meta.get("lab_run_id") or None
    lab = ledger.run(run_id) if run_id else None
    manifest = dict(meta.get("manifest") or (lab or {}).get("manifest") or {})
    stored = seed = index = None
    if lab is not None:
        trials = ledger.trials(str(run_id))
        match = next((t for t in trials if t.params == handle.params and t.status == "ok"), None)
        match = match or _best(trials, _direction(lab.get("objective")))
        if match is not None:
            stored, index = match.score, match.trial_index
        seed = lab.get("seed")
    return VerifyTarget(
        target=target,
        kind="strategy",
        class_path=handle.class_path,
        params=dict(handle.params),
        manifest=manifest,
        run_id=str(run_id) if run_id else None,
        objective=(lab or {}).get("objective"),
        stored_score=stored,
        seed=seed,
        trial_index=index,
    )


def _run_target(ledger: TrialLedger, run: Mapping[str, Any], target: str) -> VerifyTarget:
    best = _best(ledger.trials(target), _direction(run.get("objective")))
    return VerifyTarget(
        target=target,
        kind="run",
        class_path=str(run["strategy_class"]),
        params=dict(best.params) if best is not None else {},
        manifest=dict(run.get("manifest") or {}),
        run_id=target,
        objective=run.get("objective"),
        stored_score=best.score if best is not None else None,
        seed=run.get("seed"),
        trial_index=best.trial_index if best is not None else None,
    )


def verify(
    target: VerifyTarget,
    *,
    lake: Any,
    settings: Any,
    tolerance: float,
    objectives: Mapping[str, Callable[[], Any]] | None = None,
) -> VerifyReport:
    """Rerun ``target`` on ``lake`` and compare. Never raises: a step that
    fails leaves its fields ``None`` and writes ``error``."""
    manifest = target.manifest
    errors: list[str] = []
    data_changed: bool | None = None
    changed: list[str] = []
    restated: list[str] = []
    stored_fp = manifest.get("data_fingerprint")
    if isinstance(stored_fp, dict) and stored_fp.get("window"):
        try:
            current = refingerprint(lake, stored_fp)
            data_changed = current["hash"] != stored_fp.get("hash")
            if data_changed:
                changed = fingerprint_changes(stored_fp, current)
                restated = restated_tickers(stored_fp, current)
        except Exception as exc:
            errors.append(f"fingerprint: {type(exc).__name__}: {exc}")
    stored_config = manifest.get("config_hash")
    config_changed = (
        None
        if stored_config is None or settings is None
        else config_hash(settings) != stored_config
    )
    stored_sha = manifest.get("git_sha")
    now_sha = git_info().get("git_sha")
    code_changed = None if stored_sha is None or now_sha is None else stored_sha != now_sha

    current_score: float | None = None
    if target.stored_score is not None and target.objective:
        try:
            current_score = _rescore(target, lake, settings, objectives)
        except Exception as exc:
            errors.append(f"rescore: {type(exc).__name__}: {exc}")
    report = VerifyReport(
        target=target.target,
        kind=target.kind,
        class_path=target.class_path,
        run_id=target.run_id,
        objective=target.objective,
        tolerance=tolerance,
        stored_score=target.stored_score,
        current_score=current_score,
        data_changed=data_changed,
        changed_tickers=changed,
        restated_tickers=restated,
        config_changed=config_changed,
        code_changed=code_changed,
        error="; ".join(errors) or None,
    )
    _log.info(
        "lab.verify.done",
        target=target.target,
        moved=report.moved,
        delta=report.score_delta,
        data_changed=data_changed,
        changed_tickers=len(changed),
        restated_tickers=len(restated),
    )
    return report


def _rescore(
    target: VerifyTarget,
    lake: Any,
    settings: Any,
    objectives: Mapping[str, Callable[[], Any]] | None,
) -> float:
    from stonks.lab.objectives import OBJECTIVES
    from stonks.lab.parallel import _call_seeded, task_seeds
    from stonks.lab.tuning.base import _run_trial, _TrialState

    catalog = dict(objectives or OBJECTIVES)
    make = catalog.get(str(target.objective))
    if make is None:
        raise ValueError(f"unknown objective {target.objective!r}")
    dataset = _dataset(target.manifest, lake, settings)
    module_name, cls_name = target.class_path.split(":", 1)
    cls = getattr(importlib.import_module(module_name), cls_name)
    index = int(target.trial_index or 0)
    seed = task_seeds(int(target.seed), index + 1)[index] if target.seed is not None else None
    state = _TrialState(cls, make(), dataset, "lab.verify")
    outcome = _call_seeded(_run_trial, state, (index, dict(target.params)), seed)
    return float(outcome.score)


def _dataset(manifest: Mapping[str, Any], lake: Any, settings: Any) -> Any:
    """The run's :class:`LabDataset`: universe, windows and interval from the
    manifest, costs as stored, execution and construction as configured."""
    from stonks.backtest.costs import CostModelSettings
    from stonks.core.interval import Interval
    from stonks.lab.dataset import LabDataset

    summary = manifest.get("dataset") or {}
    if not summary.get("full_window") or not summary.get("train_window"):
        raise ValueError("the manifest has no dataset windows")
    start, end = (date.fromisoformat(str(d)[:10]) for d in summary["full_window"])
    train_end = date.fromisoformat(str(summary["train_window"][1])[:10])
    costs = manifest.get("costs")
    backtest = getattr(settings, "backtest", None)
    return LabDataset(
        lake=lake,
        universe=list(summary.get("universe") or []),
        start=start,
        end=end,
        train_end=train_end,
        interval=Interval.parse(str(summary.get("interval") or "1d")),
        costs=CostModelSettings.model_validate(costs) if isinstance(costs, dict) else None,
        execution=getattr(backtest, "execution", None),
        construction=getattr(backtest, "construction", None),
        universe_id=summary.get("universe_id"),
    )


def _direction(objective: Any) -> str:
    from stonks.lab.objectives import OBJECTIVES

    make = OBJECTIVES.get(str(objective))
    return str(getattr(make(), "direction", "maximize")) if make else "maximize"


def _best(trials: list[Any], direction: str) -> Any:
    sign = -1.0 if direction == "minimize" else 1.0
    ok = [t for t in trials if t.status == "ok" and math.isfinite(t.score)]
    return max(ok, key=lambda t: sign * t.score) if ok else None
