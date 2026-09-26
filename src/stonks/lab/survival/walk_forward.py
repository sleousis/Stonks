"""Walk-forward validation: re-tune on rolling train windows, score on the
out-of-sample window that follows each one.

Fold geometry (calendar days, inclusive bounds): ``n_splits`` contiguous,
non-overlapping test windows of ``test_days`` each, the last ending on the
dataset's end day. Each fold's train window ends the day before its test
window starts and is either

- **rolling** — a fixed ``train_days`` long (default: everything before
  the first test window, so the first fold trains from the dataset start),
- **anchored** — always starts at the dataset start and grows by
  ``test_days`` per fold.

Per fold the strategy class is re-tuned on the train window with the
runner's tuner / objective / budget (``TuningSetup``), fitted on the same
fold dataset, and backtested on the test window only. Test windows never
feed tuning or fitting; bars before a test window stay visible to the
strategy for look-backs, as in any backtest. Fitted state must not read
beyond the dataset's train window — the same contract as ``LabRunner``.

Passing requires both ``positive_share >= min_positive_share`` (share of
folds with an OOS score > 0) and ``oos_score_mean >= min_mean_score``.

Cost is ``n_splits * budget`` tuning backtests plus ``n_splits`` test
backtests — several times a plain lab run — so the test is opt-in: add it
to a ``SurvivalSuite`` explicitly.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.survival.base import TuningSetup
from stonks.lab.tuning.base import tune_and_fit
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.walk_forward")


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


class WalkForwardConfig(BaseModel):
    """Walk-forward settings. ``test_days=None`` splits the dataset's
    validation window evenly over the folds;
    ``train_days=None`` means "everything before the first test window"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_splits: int = Field(default=4, ge=1)
    test_days: int | None = Field(default=None, ge=1)
    train_days: int | None = Field(default=None, ge=1)
    anchored: bool = False
    #: Backtest statistic scored on each test window; 0 is neutral for all.
    metric: Literal["sharpe", "cagr", "final_return"] = "sharpe"
    min_positive_share: float = Field(default=0.5, ge=0.0, le=1.0)
    min_mean_score: float = 0.0

    def resolved_test_days(self, val_window: tuple[date, date]) -> int:
        """``test_days``, or the dataset's validation window split evenly
        over the folds — so the OOS windows cover the same days the
        runner's own tuning never saw."""
        if self.test_days is not None:
            return self.test_days
        val_start, val_end = val_window
        return max(1, ((val_end - val_start).days + 1) // self.n_splits)


def walk_forward_folds(
    start: date,
    end: date,
    *,
    n_splits: int,
    test_days: int,
    train_days: int | None = None,
    anchored: bool = False,
) -> list[WalkForwardFold]:
    """Lay ``n_splits`` folds over ``[start, end]``; see the module doc.

    Raises ``ValueError`` when the span cannot hold them with a non-empty
    train window starting on or after ``start``.
    """
    if n_splits < 1 or test_days < 1:
        raise ValueError("n_splits and test_days must be >= 1")
    first_test = end - timedelta(days=n_splits * test_days - 1)
    if first_test <= start:
        raise ValueError(
            f"{n_splits} test windows of {test_days} days leave no train window in [{start}, {end}]"
        )
    if train_days is None:
        train_days = (first_test - start).days
    folds: list[WalkForwardFold] = []
    for i in range(n_splits):
        test_start = first_test + timedelta(days=i * test_days)
        train_end = test_start - timedelta(days=1)
        train_start = start if anchored else test_start - timedelta(days=train_days)
        if train_start < start:
            raise ValueError(
                f"fold {i} would train from {train_start}, before the dataset start {start}; "
                "shorten train_days/test_days or reduce n_splits"
            )
        folds.append(
            WalkForwardFold(
                index=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_start + timedelta(days=test_days - 1),
            )
        )
    return folds


class WalkForwardTest:
    id = "walk_forward"

    def __init__(
        self, config: WalkForwardConfig | None = None, tuning: TuningSetup | None = None
    ) -> None:
        self._cfg = config or WalkForwardConfig()
        self._tuning = tuning
        self._bound: TuningSetup | None = None

    def bind_tuning(self, setup: TuningSetup) -> None:
        """Receive the runner's tuning setup (used when ``tuning`` is unset)."""
        self._bound = setup

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        setup = self._tuning or self._bound
        if setup is None:
            raise ValueError(
                "WalkForwardTest needs a tuning setup: pass tuning=... or run it under LabRunner"
            )
        cfg = self._cfg
        folds = walk_forward_folds(
            context.start,
            context.end,
            n_splits=cfg.n_splits,
            test_days=cfg.resolved_test_days(context.val_window),
            train_days=cfg.train_days,
            anchored=cfg.anchored,
        )
        strategy_cls = type(strategy)
        fixed = setup.retune_fixed_params(strategy)  # e.g. a wrapper's inner strategy
        metrics: dict[str, float] = {}
        oos_scores: list[float] = []
        is_scores: list[float] = []
        for fold in folds:
            fold_ds = dataclasses.replace(
                context, start=fold.train_start, end=fold.test_end, train_end=fold.train_end
            )
            fitted, tuned = tune_and_fit(strategy_cls, fold_ds, setup, fixed)
            report = run_backtest(fitted, fold_ds, (fold.test_start, fold.test_end))
            oos = float(getattr(report, cfg.metric))
            oos_scores.append(oos)
            is_scores.append(float(tuned.best_score))
            metrics[f"fold{fold.index}_oos_score"] = oos
            metrics[f"fold{fold.index}_is_score"] = float(tuned.best_score)
            metrics[f"fold{fold.index}_max_drawdown_oos"] = float(report.max_drawdown)
            _log.info(
                "walk_forward.fold",
                fold=fold.index,
                train=[str(fold.train_start), str(fold.train_end)],
                test=[str(fold.test_start), str(fold.test_end)],
                best_params=dict(tuned.best_params),
                is_score=tuned.best_score,
                oos_score=oos,
            )

        finite = [s for s in oos_scores if math.isfinite(s)]
        mean_oos = sum(finite) / len(finite) if finite else 0.0
        positive_share = sum(1 for s in oos_scores if s > 0) / len(oos_scores)
        finite_is = [s for s in is_scores if math.isfinite(s)]
        metrics.update(
            {
                "oos_score_mean": mean_oos,
                "positive_share": positive_share,
                "is_score_mean": sum(finite_is) / len(finite_is) if finite_is else 0.0,
                "n_folds": float(len(folds)),
            }
        )
        passed = positive_share >= cfg.min_positive_share and mean_oos >= cfg.min_mean_score
        windows = "; ".join(
            f"{f.index}: train {f.train_start}..{f.train_end} test {f.test_start}..{f.test_end}"
            for f in folds
        )
        mode = "anchored" if cfg.anchored else "rolling"
        return SurvivalReport(
            test_id=self.id,
            passed=passed,
            metrics=metrics,
            notes=f"metric={cfg.metric}; {mode}; {windows}",
        )
