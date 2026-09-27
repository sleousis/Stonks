"""Bayesian search with Optuna (22.1), wrapped behind the ``Tuner`` seam.

Optuna proposes, our lab pool evaluates. The study runs in ask-and-tell
mode: each round asks for ``batch_size`` trials, builds, fits and scores
them through ``lab.tuning.base.evaluate_candidates`` (the one process
pool), then tells Optuna the scores. ``batch_size`` is a setting, not the
worker count, and every trial's global RNGs are seeded from the round and
its place in it, so the result is the same for any number of workers. The
sampler is seeded, so the same seed gives the same trials.

Samplers:

- ``tpe`` (default): the tree-structured Parzen estimator. The first
  ``n_startup_trials`` are random, then it samples near the good trials.
- ``nsga2``: a genetic Pareto search. With an objective that names several
  components (``metric_names`` and ``directions``, see
  ``lab.objectives.MultiMetricObjective``) the study searches them jointly
  and the winner is the best weighted score on the Pareto front. Other
  objectives run as one value.
- ``random``: Optuna's seeded random sampler, a baseline.

Pruning (``prune=True``) makes sense only where a cheap early score
exists. For a strategy with the vectorised fast path
(``target_positions``, ``lab/vectorized.py``) each trial first reports its
fast score and a median pruner stops the trials that trail the finished
ones before their full backtest. Other strategies are never pruned.

Every trial counts (P2): a pruned trial comes back as a failed
:class:`~stonks.core.protocols.TrialOutcome` (NaN score, error starting
with :data:`PRUNED`), so the trial ledger, the deflated Sharpe and PBO see
every set that was tried. No Optuna type leaves this module.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import optuna
from optuna.distributions import (
    BaseDistribution,
    CategoricalDistribution,
    FloatDistribution,
    IntDistribution,
)
from optuna.trial import TrialState

from stonks.core.params import ParameterSpec, ParamSpace, tunable_only
from stonks.core.protocols import Objective, Strategy, TrialOutcome, TunerResult
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning.base import best_of, evaluate_candidates, merge_with_defaults
from stonks.logging import get_logger

_log = get_logger("stonks.lab.tuning.optuna")

__all__ = ["PRUNED", "OptunaTuner", "SamplerName"]

SamplerName = Literal["tpe", "nsga2", "random"]
_SAMPLERS: tuple[str, ...] = ("tpe", "nsga2", "random")

#: Error prefix of a trial the pruner stopped before its full backtest.
PRUNED = "pruned"

#: Spreads the per-round root seeds apart (a large prime).
_ROUND_STRIDE = 1_000_003


@dataclass(frozen=True)
class _Axis:
    """One tunable parameter as an Optuna distribution. Categorical choices
    are addressed by index so any value (not only str or numbers) works."""

    spec: ParameterSpec
    distribution: BaseDistribution
    choices: tuple[Any, ...] | None = None

    def value(self, raw: Any) -> Any:
        if self.choices is not None:
            return self.choices[int(raw)]
        if self.spec.kind == "int":
            return int(raw)
        return float(raw)


def _axes(space: ParamSpace) -> list[_Axis]:
    out: list[_Axis] = []
    for spec in tunable_only(space):
        if spec.kind == "bool":
            choices: tuple[Any, ...] = (False, True)
        elif spec.kind == "categorical":
            if not spec.bounds:
                continue  # an open choice (a ticker): stays at its default
            choices = tuple(spec.bounds)
        else:
            if spec.bounds is None:
                raise ValueError(f"numeric parameter {spec.name!r} needs bounds")
            lo, hi = spec.bounds
            dist: BaseDistribution = (
                IntDistribution(int(lo), int(hi))
                if spec.kind == "int"
                else FloatDistribution(float(lo), float(hi))
            )
            out.append(_Axis(spec, dist))
            continue
        out.append(_Axis(spec, CategoricalDistribution(list(range(len(choices)))), choices))
    return out


class OptunaTuner:
    """Optuna's samplers behind the ``Tuner`` protocol (module doc).

    ``batch_size`` trials are asked per round and evaluated in parallel;
    ``n_startup_trials`` random trials seed TPE (default: one batch, at
    least 5). ``prune`` turns on the median pruner on the fast score after
    ``prune_startup`` finished trials; ``prune_cost_bps`` charges that
    fast score a flat cost per unit turnover."""

    def __init__(
        self,
        seed: int = 0,
        parallel: ParallelSettings | None = None,
        *,
        sampler: SamplerName = "tpe",
        batch_size: int = 8,
        n_startup_trials: int | None = None,
        prune: bool = False,
        prune_startup: int = 5,
        prune_cost_bps: float = 5.0,
    ) -> None:
        if sampler not in _SAMPLERS:
            raise ValueError(f"sampler must be one of {_SAMPLERS}, got {sampler!r}")
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if n_startup_trials is not None and n_startup_trials < 0:
            raise ValueError("n_startup_trials must be >= 0")
        if prune_startup < 1 or prune_cost_bps < 0:
            raise ValueError("prune_startup must be >= 1 and prune_cost_bps >= 0")
        #: Seeds the sampler and roots the per-trial seeds (the manifest reads it).
        self.seed = seed
        self._parallel = parallel or ParallelSettings()
        self._sampler = sampler
        self._batch_size = batch_size
        self._n_startup = n_startup_trials
        self._prune = prune
        self._prune_startup = prune_startup
        self._prune_cost_bps = prune_cost_bps

    def tune(
        self,
        strategy_cls: type[Strategy],
        param_space: ParamSpace,
        objective: Objective,
        dataset: Any,
        budget: int,
    ) -> TunerResult:
        axes = _axes(param_space)
        pareto = self._pareto_names(objective)
        screen = self._screen(strategy_cls, dataset) if self._prune and not pareto else None
        _log.info(
            "optuna.tune.start",
            sampler=self._sampler,
            seed=self.seed,
            budget=budget,
            batch_size=self._batch_size,
            pareto=pareto,
            prune=screen is not None,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", optuna.exceptions.ExperimentalWarning)
            optuna.logging.set_verbosity(optuna.logging.WARNING)
            study = self._study(objective, pareto)
            trials, by_number = self._search(
                study, axes, strategy_cls, param_space, objective, dataset, budget, pareto, screen
            )
            best_params, best_score = self._best(
                study, trials, by_number, objective, param_space, pareto
            )
        n_pruned = sum(1 for t in trials if (t.error or "").startswith(PRUNED))
        _log.info("optuna.tune.done", trials=len(trials), pruned=n_pruned, best_score=best_score)
        return TunerResult(
            best_params=best_params,
            best_score=best_score,
            history=[(t.params, t.score) for t in trials],
            trials=trials,
        )

    # ---- study ------------------------------------------------------------

    def _pareto_names(self, objective: Objective) -> list[str] | None:
        if self._sampler != "nsga2":
            return None
        names = getattr(objective, "metric_names", None)
        directions = getattr(objective, "directions", None)
        if not names or not directions or len(names) != len(directions):
            return None
        return [str(n) for n in names]

    def _study(self, objective: Objective, pareto: list[str] | None) -> optuna.Study:
        startup = self._n_startup if self._n_startup is not None else max(5, self._batch_size)
        sampler: optuna.samplers.BaseSampler
        if self._sampler == "tpe":
            sampler = optuna.samplers.TPESampler(seed=self.seed, n_startup_trials=startup)
        elif self._sampler == "nsga2":
            sampler = optuna.samplers.NSGAIISampler(
                seed=self.seed, population_size=max(2, self._batch_size)
            )
        else:
            sampler = optuna.samplers.RandomSampler(seed=self.seed)
        pruner = optuna.pruners.MedianPruner(n_startup_trials=self._prune_startup)
        if pareto is not None:
            directions = list(getattr(objective, "directions"))  # noqa: B009 - checked above
            return optuna.create_study(sampler=sampler, directions=directions)
        return optuna.create_study(sampler=sampler, pruner=pruner, direction=objective.direction)

    def _search(
        self,
        study: optuna.Study,
        axes: Sequence[_Axis],
        strategy_cls: type[Strategy],
        param_space: ParamSpace,
        objective: Objective,
        dataset: Any,
        budget: int,
        pareto: list[str] | None,
        screen: _Screen | None,
    ) -> tuple[list[TrialOutcome], dict[int, int]]:
        distributions = {a.spec.name: a.distribution for a in axes}
        sign = 1.0 if objective.direction == "maximize" else -1.0
        trials: list[TrialOutcome] = []
        by_number: dict[int, int] = {}  # Optuna trial number -> index in trials
        round_no = 0
        while len(trials) < budget:
            asked = [
                study.ask(dict(distributions))
                for _ in range(min(self._batch_size, budget - len(trials)))
            ]
            candidates = [
                merge_with_defaults(
                    {a.spec.name: a.value(t.params[a.spec.name]) for a in axes}, param_space
                )
                for t in asked
            ]
            outcomes: list[TrialOutcome | None] = [None] * len(asked)
            if screen is not None:
                for i, (trial, params) in enumerate(zip(asked, candidates, strict=True)):
                    fast = screen.score(params)
                    if not math.isfinite(fast):
                        continue
                    trial.report(sign * fast, step=0)
                    if trial.should_prune():
                        study.tell(trial, state=TrialState.PRUNED)
                        outcomes[i] = TrialOutcome.failed(
                            params, f"{PRUNED} (fast score {fast:.4g})"
                        )
            todo = [i for i, o in enumerate(outcomes) if o is None]
            full = evaluate_candidates(
                strategy_cls,
                [candidates[i] for i in todo],
                objective,
                dataset,
                parallel=self._parallel,
                root_seed=self.seed + _ROUND_STRIDE * round_no,
                log_prefix="optuna",
            )
            for i, outcome in zip(todo, full, strict=True):
                outcomes[i] = outcome
                self._tell(study, asked[i], outcome, pareto)
            for trial, outcome in zip(asked, outcomes, strict=True):
                assert outcome is not None
                by_number[trial.number] = len(trials)
                trials.append(outcome)
            round_no += 1
        return trials, by_number

    @staticmethod
    def _tell(
        study: optuna.Study, trial: Any, outcome: TrialOutcome, pareto: list[str] | None
    ) -> None:
        if outcome.status != "ok":
            study.tell(trial, state=TrialState.FAIL)
            return
        if pareto is None:
            if math.isfinite(outcome.score):
                study.tell(trial, float(outcome.score))
            else:
                study.tell(trial, state=TrialState.FAIL)
            return
        metrics = outcome.metrics or {}
        values = [metrics.get(name, math.nan) for name in pareto]
        if all(isinstance(v, int | float) and math.isfinite(v) for v in values):
            study.tell(trial, [float(v) for v in values])
        else:
            study.tell(trial, state=TrialState.FAIL)

    @staticmethod
    def _best(
        study: optuna.Study,
        trials: list[TrialOutcome],
        by_number: dict[int, int],
        objective: Objective,
        param_space: ParamSpace,
        pareto: list[str] | None,
    ) -> tuple[dict[str, Any], float]:
        """The best trial by score; with a Pareto study, the best score among
        the Pareto front."""
        if pareto is not None:
            front = [trials[by_number[t.number]] for t in study.best_trials]
            if front:
                return best_of(front, objective.direction, param_space)
        return best_of(trials, objective.direction, param_space)

    # ---- fast score for pruning -------------------------------------------

    def _screen(self, strategy_cls: type[Strategy], dataset: Any) -> _Screen | None:
        from stonks.lab.vectorized import load_closes, supports_vectorized

        lake = getattr(dataset, "lake", None)
        window = getattr(dataset, "train_window", None)
        if not supports_vectorized(strategy_cls) or lake is None or window is None:
            if self._prune:
                _log.info("optuna.prune.unavailable", strategy=strategy_cls.__name__)
            return None
        closes = load_closes(lake, list(getattr(dataset, "universe", []) or []), window[1])
        if closes.empty:
            return None
        return _Screen(strategy_cls, closes, window[0], self._prune_cost_bps)


@dataclass(frozen=True)
class _Screen:
    strategy_cls: type[Any]
    closes: Any
    start: Any
    cost_bps: float

    def score(self, params: dict[str, Any]) -> float:
        from stonks.lab.vectorized import prescreen

        [screened] = prescreen(
            self.strategy_cls, [params], self.closes, start=self.start, cost_bps=self.cost_bps
        )
        return float(screened.score)
