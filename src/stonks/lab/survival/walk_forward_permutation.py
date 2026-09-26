"""Walk-forward permutation test (Masters' walk-forward MCPT).

Asks whether the whole walk-forward process — re-tune on each train
window, trade the next test window — beats the same process run on noise.

- **Real score:** the walk-forward OOS score, i.e. ``oos_score_mean`` of a
  :class:`~stonks.lab.survival.walk_forward.WalkForwardTest` run with the
  runner's tuning setup.
- **Null:** the same walk-forward run on ``n_permutations`` lakes in which
  every bar from the first fold's test window on is permuted
  (:func:`~stonks.lab.survival.permutation.permute_bars_together`, one
  shared ordering for the universe). Bars up to the start of the first
  test window stay real: the first training window and the embargo gap
  after it. So the first fold tunes on true history and only what
  walk-forward trades on, and re-tunes on later, is noise. Bars after the
  dataset end are dropped.
- **No evidence fails:** a dataset too short for the folds, or a real run
  with no finite OOS score, fails with "insufficient data".
- **p-value:** ``(count(null >= real) + 1) / (n_permutations + 1)``; the
  test passes when ``p_value <= max_p_value``.

Real and permuted runs go through the same in-memory lake construction as
the MCPT (``PermutationScorer``), and permutations run on the lab process
pool (``lab.parallel``) with seeds drawn up front, so the report does not
depend on ``max_workers``.

Cost is ``(n_permutations + 1) * n_splits * budget`` tuning backtests, so
the test is opt-in: add it to a ``SurvivalSuite`` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.permutation import (
    PermutationScorer,
    permutation_p_value,
    permuted_scores,
)
from stonks.lab.survival.walk_forward import WalkForwardConfig, WalkForwardTest
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.walk_forward_permutation")


class WalkForwardPermutationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Fold geometry and OOS metric of every walk-forward run (real and null).
    walk_forward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    n_permutations: int = Field(default=20, ge=1)
    max_p_value: float = Field(default=0.05, gt=0.0, le=1.0)
    seed: int | None = 17
    #: Worker processes for the permutations; ``None`` means
    #: ``lab.parallel.default_max_workers()``.
    max_workers: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class WalkForwardScore:
    """Evaluator: the OOS mean of a walk-forward run on the dataset."""

    config: WalkForwardConfig
    setup: TuningSetup

    def __call__(self, strategy: Strategy, dataset: Any) -> float:
        report = WalkForwardTest(self.config, self.setup).run(strategy, dataset)
        return float(report.metrics["oos_score_mean"])


class WalkForwardPermutationTest:
    id = "walk_forward_mcpt"

    def __init__(
        self,
        config: WalkForwardPermutationConfig | None = None,
        tuning: TuningSetup | None = None,
    ) -> None:
        self._cfg = config or WalkForwardPermutationConfig()
        self._tuning = tuning
        self._bound: TuningSetup | None = None

    def bind_tuning(self, setup: TuningSetup) -> None:
        """Receive the runner's tuning setup (used when ``tuning`` is unset)."""
        self._bound = setup

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        setup = self._tuning or self._bound
        if setup is None:
            raise ValueError(
                "WalkForwardPermutationTest needs a tuning setup: "
                "pass tuning=... or run it under LabRunner"
            )
        cfg = self._cfg
        try:
            folds = cfg.walk_forward.folds_for(context, strategy)
        except ValueError as exc:  # RS-26: too short a dataset fails, never crashes
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={
                    "p_value": 1.0,
                    "n_permutations": float(cfg.n_permutations),
                    "n_folds": 0.0,
                },
                notes=f"insufficient data: {exc}",
            )
        # permutable = every bar from the first test window on (the
        # embargo gap before it stays real, like the training window)
        window = (folds[0].test_start, context.end)
        _log.info(
            "walk_forward_mcpt.start",
            seed=cfg.seed,
            n=cfg.n_permutations,
            real_until=str(folds[0].train_end),
        )
        scorer = PermutationScorer.build(
            strategy, context, window, WalkForwardScore(cfg.walk_forward, setup)
        )
        if scorer is None:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={
                    "p_value": 1.0,
                    "real_score": 0.0,
                    "n_permutations": float(cfg.n_permutations),
                    "n_folds": float(len(folds)),
                },
                notes="no bars after the first training window for the universe; test skipped",
            )

        real_score = scorer.score_real()
        if not np.isfinite(real_score):
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={
                    "p_value": 1.0,
                    "real_score": float(real_score),
                    "n_permutations": float(cfg.n_permutations),
                    "n_folds": float(len(folds)),
                },
                notes="insufficient data: the real walk-forward run has no finite OOS score",
            )
        perm = permuted_scores(scorer, cfg.n_permutations, cfg.seed, cfg.max_workers)
        p_value = permutation_p_value(real_score, perm)
        arr = np.asarray(perm, dtype=float)
        return SurvivalReport(
            test_id=self.id,
            passed=p_value <= cfg.max_p_value,
            metrics={
                "p_value": float(p_value),
                "real_score": float(real_score),
                "perm_score_mean": float(arr.mean()),
                "perm_score_max": float(arr.max()),
                "n_permutations": float(cfg.n_permutations),
                "n_folds": float(len(folds)),
            },
            notes=(
                f"metric={cfg.walk_forward.metric}; seed={cfg.seed}; "
                f"real bars until {folds[0].train_end}, permuted after"
            ),
        )
