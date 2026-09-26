"""Lab orchestration: preflight → pre-register → tune → fit → survival suite → verdict.

Every run gets a ``run_id`` and a reproducibility manifest (BL-06). The
suite runs on a fresh copy of the dataset embargoed for the fitted
strategy (:func:`suite_dataset`). With a
:class:`~stonks.lab.trials.TrialLedger` the run is pre-registered (hypothesis,
premortem, tuner, budget, dataset, manifest) **before** tuning, every trial
is recorded after it, and the verdict at the end (``error`` when it
crashes). Before any of that the BL-37 preflight
(:func:`~stonks.lab.preflight.run_preflight`) checks data coverage, audit
flags and universe membership: its errors stop the run, its warnings are
logged and stored in the manifest. A dataset with a ``universe_id`` and no
tickers first gets that universe's members over the window, and with a
``data_ensurer`` their missing bars are fetched before the preflight
(:func:`~stonks.lab.universe_data.prepare_dataset`). Survival tests with a ``bind_run(ctx)`` hook receive a
:class:`~stonks.lab.trials.LabRunContext` before the suite runs.
"""

from __future__ import annotations

import dataclasses
import math
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
from stonks.lab.preflight import PreflightError, PreflightReport, run_preflight
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
from stonks.lab.universe_data import prepare_dataset
from stonks.logging import get_logger
from stonks.strategies.base import strategy_data_tickers

_log = get_logger("stonks.lab.runner")


def costs_are_zero(costs: Any) -> bool:
    """True when a ``CostModelSettings`` (or ``None``) charges nothing: no
    impact and no fee or spread for any asset class (BL-13)."""
    if costs is None:
        return True
    if getattr(costs, "impact_bps", 0.0):
        return False
    classes = [costs.default, *getattr(costs, "asset_classes", {}).values()]
    return not any(c.fee_flat or c.half_spread_bps or c.fee_bps for c in classes)


def with_strategy_references(
    dataset: Any, strategy_cls: type[Strategy], params: Mapping[str, Any] | None = None
) -> Any:
    """``dataset`` with the tickers ``strategy_cls(params)`` reads but does
    not trade added to its ``reference_tickers`` (RS-01), so the preflight
    checks them and every snapshot and modified lake copies them. Unchanged
    when the dataset has no such field or the strategy can't be built here
    (tuning then reports the error)."""
    with_references = getattr(dataset, "with_references", None)
    if not callable(with_references):
        return dataset
    try:
        strategy = strategy_cls(dict(params or {}))
    except Exception as exc:  # the tuner builds it again and reports the error
        _log.info("lab.references.unavailable", error=str(exc))
        return dataset
    return with_references(strategy_data_tickers(strategy))


def suite_dataset(dataset: Any, strategy: Strategy) -> Any:
    """What the survival suite runs on: a fresh copy of ``dataset`` (so a
    per-run attribute such as ``stitched_oos_report`` never leaks into the
    next run) with the embargo ``strategy``'s label horizon needs
    (``LabDataset.for_strategy``, BL-20)."""
    if dataclasses.is_dataclass(dataset) and not isinstance(dataset, type):
        dataset = dataclasses.replace(dataset)
    for_strategy = getattr(dataset, "for_strategy", None)
    return for_strategy(strategy) if callable(for_strategy) else dataset


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
    #: The BL-37 preflight report (``None`` when turned off or it crashed).
    preflight: PreflightReport | None = None

    @property
    def artifact_meta(self) -> dict[str, Any]:
        """Lab provenance for the registered artifact's ``meta.json``
        (``ArtifactBundle(meta=...)`` or ``registry.artifact.update_meta``)."""
        meta: dict[str, Any] = {
            "lab_run_id": self.run_id,
            "n_trials_total": self.n_trials_class,
            "hypothesis": self.hypothesis,
            "premortem": self.premortem,
            "manifest": self.manifest,
        }
        meta.update(self.ic_meta)
        return meta

    @property
    def ic_meta(self) -> dict[str, Any]:
        """``ic_estimate`` (and its horizon) from the ``signal_ic`` report,
        for BL-08's alpha normalisation; empty when the run had none."""
        for report in self.survival_reports:
            if report.test_id != "signal_ic":
                continue
            estimate = report.metrics.get("ic_estimate")
            if estimate is None or not math.isfinite(estimate):
                return {}
            return {
                "ic_estimate": float(estimate),
                "ic_horizon": int(report.metrics.get("ic_horizon") or 0),
            }
        return {}


class LabRunner:
    def __init__(
        self,
        tuner: Tuner,
        objective: Objective,
        suite: SurvivalSuite,
        budget: int = 20,
        ledger: TrialLedger | None = None,
        settings: Any = None,
        *,
        preflight: bool = True,
        strict_preflight: bool = False,
        data_ensurer: Any = None,
    ) -> None:
        """``ledger`` persists runs and trials (``None``: in-memory only);
        ``settings`` feeds the manifest's config hash and default costs.
        ``preflight`` runs the BL-37 data checks first (errors stop the
        run, warnings are logged); ``strict_preflight`` makes every
        warning an error. ``data_ensurer`` (a
        :class:`~stonks.ingest.ensure.DataEnsurer`, opt in) fetches the
        universe's missing bars before the preflight."""
        self._tuner = tuner
        self._objective = objective
        self._suite = suite
        self._budget = budget
        self._ledger = ledger
        self._settings = settings
        self._preflight = preflight
        self._strict_preflight = strict_preflight
        self._data_ensurer = data_ensurer

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
        dataset, ensured = prepare_dataset(
            dataset, ensurer=self._data_ensurer, strategy=strategy_cls
        )
        dataset = with_strategy_references(dataset, strategy_cls, fixed_params)
        preflight, preflight_record = self._run_preflight(strategy_cls, dataset, class_path)
        seeds = collect_seeds(self._tuner, self._suite.tests)
        manifest = self._manifest(dataset, seeds)
        if ensured is not None:
            manifest["ensure"] = ensured.model_dump(mode="json")
        if preflight_record is not None:
            manifest["preflight"] = preflight_record
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
        if costs_are_zero(getattr(dataset, "costs", None)):
            # BL-13: a cost-free backtest overstates every edge.
            _log.warning(
                "lab.zero_costs",
                run_id=run_id,
                strategy=class_path,
                hint="every backtest of this run ignores fees, spread and impact; "
                "configure [backtest.costs] or pass a cost model",
            )
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
        result.preflight = preflight
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

        reports = self._suite.run(strategy, suite_dataset(dataset, strategy))
        # RS-39: an empty suite tested nothing, so it can't pass
        verdict = "pass" if reports and all(r.passed for r in reports) else "fail"

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

    def _run_preflight(
        self, strategy_cls: type[Strategy], dataset: LabDataset, class_path: str
    ) -> tuple[PreflightReport | None, dict[str, Any] | None]:
        """The preflight report and its manifest record. Raises
        :class:`PreflightError` on errors. A preflight that crashes never
        blocks the run: it is logged and recorded as an error string."""
        if not self._preflight:
            return None, None
        try:
            report = run_preflight(dataset, strategy_cls, strict=self._strict_preflight)
        except Exception as exc:
            _log.warning("lab.preflight.failed", strategy=class_path, error=str(exc))
            return None, {"error": str(exc)}
        for issue in report.warnings:
            _log.warning(
                "lab.preflight.warning", strategy=class_path, code=issue.code, hint=issue.message
            )
        if not report.ok:
            _log.error(
                "lab.preflight.error",
                strategy=class_path,
                codes=[i.code for i in report.errors],
            )
            raise PreflightError(report)
        return report, report.to_dict()

    def _manifest(self, dataset: LabDataset, seeds: dict[str, Any]) -> dict[str, Any]:
        try:
            return build_manifest(self._settings, dataset, seeds)
        except Exception as exc:  # provenance must never block a lab run
            _log.warning("lab.manifest.failed", error=str(exc))
            return {"seeds": seeds, "error": str(exc)}
