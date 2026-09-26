"""Lab orchestration: pre-register → tune → fit → survival suite → verdict.

Every run gets a ``run_id`` and a reproducibility manifest (BL-06). With a
:class:`~stonks.lab.trials.TrialLedger` the run is pre-registered (hypothesis,
premortem, tuner, budget, dataset, manifest) **before** tuning, every trial
is recorded after it, and the verdict at the end (``error`` when it
crashes). Survival tests with a ``bind_run(ctx)`` hook receive a
:class:`~stonks.lab.trials.LabRunContext` before the suite runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from stonks.core.protocols import (
    Objective,
    Strategy,
    SurvivalReport,
    Tuner,
)
from stonks.lab.dataset import LabDataset
from stonks.lab.manifest import build_manifest, collect_seeds
from stonks.lab.survival.base import SurvivalSuite, TuningSetup
from stonks.lab.trials import (
    LabRunContext,
    LabRunSpec,
    TrialLedger,
    TrialMatrix,
    TrialRecord,
    new_run_id,
    trials_from_tuning,
)
from stonks.lab.tuning.base import tune_and_fit
from stonks.logging import get_logger

_log = get_logger("stonks.lab.runner")


@dataclass
class LabRunResult:
    strategy_cls: type[Strategy]
    best_params: dict[str, Any]
    best_score: float
    strategy: Strategy
    survival_reports: list[SurvivalReport]
    verdict: str  # "pass" | "fail"
    run_id: str = ""
    #: The tuner's ``(params, score)`` trials, in evaluation order.
    history: list[tuple[dict[str, Any], float]] = field(default_factory=list)
    trials: list[TrialRecord] = field(default_factory=list)
    #: Per-bar trial returns (T x N) when the tuner reported them.
    trial_matrix: TrialMatrix | None = None
    n_trials_run: int = 0
    #: Cumulative trials of this strategy class across ledgered runs
    #: (equals ``n_trials_run`` without a ledger).
    n_trials_class: int = 0
    hypothesis: str | None = None
    premortem: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def artifact_meta(self) -> dict[str, Any]:
        """Lab provenance for the registered artifact's ``meta.json``
        (``ArtifactBundle(meta=...)`` or ``registry.artifact.update_meta``)."""
        return {
            "lab_run_id": self.run_id,
            "n_trials_total": self.n_trials_class,
            "hypothesis": self.hypothesis,
            "premortem": self.premortem,
            "manifest": self.manifest,
        }


class LabRunner:
    def __init__(
        self,
        tuner: Tuner,
        objective: Objective,
        suite: SurvivalSuite,
        budget: int = 20,
        ledger: TrialLedger | None = None,
        settings: Any = None,
    ) -> None:
        """``ledger`` persists runs and trials (``None``: in-memory only);
        ``settings`` feeds the manifest's config hash and default costs."""
        self._tuner = tuner
        self._objective = objective
        self._suite = suite
        self._budget = budget
        self._ledger = ledger
        self._settings = settings

    def run(
        self,
        strategy_cls: type[Strategy],
        dataset: LabDataset,
        fixed_params: Mapping[str, Any] | None = None,
        *,
        hypothesis: str | None = None,
        premortem: str | None = None,
    ) -> LabRunResult:
        """``fixed_params`` pin params for tuning (e.g. a
        ``MacroRegimeFilter``'s inner strategy, a ticker); survival tests
        that re-tune keep them pinned, plus the strategy's non-tunable params.
        ``hypothesis`` / ``premortem`` are recorded before tuning starts."""
        class_path = f"{strategy_cls.__module__}:{strategy_cls.__name__}"
        seeds = collect_seeds(self._tuner, self._suite.tests)
        manifest = self._manifest(dataset, seeds)
        run_id = new_run_id()
        if self._ledger is not None:
            tuner_seed = seeds.get("tuner")
            self._ledger.start_run(
                LabRunSpec(
                    strategy_class=class_path,
                    hypothesis=hypothesis,
                    premortem=premortem,
                    tuner=type(self._tuner).__name__,
                    objective=str(getattr(self._objective, "name", type(self._objective).__name__)),
                    budget=self._budget,
                    seed=tuner_seed if isinstance(tuner_seed, int) else None,
                    dataset=manifest.get("dataset") or {},
                    manifest=manifest,
                ),
                run_id=run_id,
            )
        _log.info("lab.run.start", run_id=run_id, strategy=class_path, hypothesis=hypothesis)
        try:
            result = self._run(strategy_cls, dataset, fixed_params, run_id, class_path, manifest)
        except BaseException:
            if self._ledger is not None:
                try:
                    self._ledger.finish_run(run_id, "error")
                except Exception as exc:  # keep the run's own exception
                    _log.warning("lab.ledger.finish_failed", run_id=run_id, error=str(exc))
            raise
        result.hypothesis, result.premortem = hypothesis, premortem
        if self._ledger is not None:
            self._ledger.finish_run(run_id, "pass" if result.verdict == "pass" else "fail")
        return result

    def _run(
        self,
        strategy_cls: type[Strategy],
        dataset: LabDataset,
        fixed_params: Mapping[str, Any] | None,
        run_id: str,
        class_path: str,
        manifest: dict[str, Any],
    ) -> LabRunResult:
        setup = TuningSetup(
            tuner=self._tuner,
            objective=self._objective,
            budget=self._budget,
            fixed_params=dict(fixed_params or {}),
        )
        strategy, tuned = tune_and_fit(strategy_cls, dataset, setup, setup.fixed_params)
        trials, matrix = trials_from_tuning(tuned)
        if self._ledger is not None:
            self._ledger.record_trials(run_id, trials, matrix)
            n_class = self._ledger.n_trials(class_path)
        else:
            n_class = len(trials)
        _log.info(
            "lab.tune.done",
            run_id=run_id,
            best_params=tuned.best_params,
            best_score=tuned.best_score,
            trials=len(trials),
            trials_class=n_class,
        )

        # Tests that re-tune (walk-forward, re-tuning MCPT) use the same
        # tuner / objective / budget that picked ``strategy``; tests that
        # judge the search itself (deflated Sharpe, PBO) get the trials.
        ctx = LabRunContext(
            setup=setup,
            run_id=run_id,
            ledger=self._ledger,
            trials=trials,
            trial_matrix=matrix,
            n_trials_run=len(trials),
            n_trials_class=n_class,
        )
        for test in self._suite.tests:
            bind = getattr(test, "bind_tuning", None)
            if callable(bind):
                bind(setup)
            bind_run = getattr(test, "bind_run", None)
            if callable(bind_run):
                bind_run(ctx)

        reports = self._suite.run(strategy, dataset)
        verdict = "pass" if all(r.passed for r in reports) else "fail"

        _log.info(
            "lab.survival.done",
            run_id=run_id,
            verdict=verdict,
            reports=[{"test": r.test_id, "passed": r.passed} for r in reports],
        )

        return LabRunResult(
            strategy_cls=strategy_cls,
            best_params=dict(tuned.best_params),
            best_score=tuned.best_score,
            strategy=strategy,
            survival_reports=list(reports),
            verdict=verdict,
            run_id=run_id,
            history=[(dict(p), float(s)) for p, s in tuned.history],
            trials=trials,
            trial_matrix=matrix,
            n_trials_run=len(trials),
            n_trials_class=n_class,
            manifest=manifest,
        )

    def _manifest(self, dataset: LabDataset, seeds: dict[str, Any]) -> dict[str, Any]:
        try:
            return build_manifest(self._settings, dataset, seeds)
        except Exception as exc:  # provenance must never block a lab run
            _log.warning("lab.manifest.failed", error=str(exc))
            return {"seeds": seeds, "error": str(exc)}
