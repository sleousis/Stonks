"""Walk-forward validation: re-tune on rolling train windows, score on the
out-of-sample window that follows each one (BL-20).

Fold geometry (calendar days, inclusive bounds): ``n_splits`` contiguous,
non-overlapping test windows of ``test_days`` each, the last ending on the
dataset's end day. Each fold's train window ends ``embargo`` days before
its test window starts (the day before when there is no embargo) and is
either

- **rolling** — a fixed ``train_days`` long (default: everything before
  the first fold's embargo, so the first fold trains from the dataset
  start),
- **anchored** — always starts at the dataset start and grows by
  ``test_days`` per fold.

Embargo: the strategy's effective embargo
(``max(dataset.embargo_bars, strategy.label_horizon_bars)``, see
``lab.dataset``) converted to calendar days. The bars in the gap are
neither tuned on nor scored, and each fold dataset carries the same
embargo, so its ``val_window`` is exactly the fold's test window.

Sessions (roadmap 21.3.1): on a dataset split by session (``sessions``
set, every intraday lab dataset) the folds count whole trading sessions
instead of calendar days (:func:`session_walk_forward_folds`).
``test_days`` and ``train_days`` then mean sessions, the embargo is whole
sessions (``lab.dataset.bars_to_sessions``) and the matrix converts its
bar counts to sessions. Each fold dataset keeps the sessions, so its
``val_window`` is still exactly the fold's test window.

Per fold the strategy class is re-tuned on the train window with the
runner's tuner / objective / budget (``TuningSetup``), fitted on the same
fold dataset, backtested on the train window (in-sample, for the
walk-forward efficiency) and on the test window (out-of-sample). Test
windows never feed tuning or fitting; bars before a test window stay
visible to the strategy for look-backs, as in any backtest. Fitted state
must not read beyond the dataset's train window — the same contract as
``LabRunner``.

Folds run as ``lab.parallel.run_tasks`` tasks (each fold's re-tune runs
in-process inside its worker; pools never nest) on ``max_workers``
processes over a lake snapshot. Tasks depend only on their fold, so the
report is identical for any worker count.

Stitching: the folds' OOS equity curves are chained into one series
(each fold rescaled to start where the previous one ended; the trades'
cash figures rescaled with it) and scored as one: ``sharpe_stitched``,
``psr0_stitched`` (the probabilistic Sharpe ratio against 0 of the
concatenated per-bar returns, with their skew, kurtosis and lag-1
autocorrelation) and ``cagr_oos_stitched``. The stitched
``BacktestReport`` is left on the dataset as ``stitched_oos_report`` for
the tests that run after this one (``mc_trades``).

Walk-forward efficiency (Davey): ``wfe = cagr_oos_stitched / mean fold IS
CAGR``, the share of the in-sample annualised return that survived out of
sample. It is NaN (and fails the gate) when the in-sample return is not
positive.

Passing requires all of

- ``positive_share >= min_positive_share`` (share of folds with an OOS
  score > 0),
- ``oos_score_mean >= min_mean_score``,
- ``wfe >= min_wfe`` (default 0.5; Davey's useful range is 0.3–1.0;
  ``None`` turns the gate off),
- with ``matrix=True``: at least ``matrix_min_pass_share`` of the matrix
  cells pass the three rules above. A cell is a whole walk-forward with
  train × test lengths from ``matrix_train_bars`` × ``matrix_test_bars``
  (trading bars, default {504, 756, 1008} × {126, 252}); cells that do not
  fit the dataset are skipped. Every cell's folds go into the same task
  list as the base run's.

Cost is ``n_splits * budget`` tuning backtests plus ``2 * n_splits``
scoring backtests (times the number of matrix cells) — several times a
plain lab run.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.backtest import metrics as bt_metrics
from stonks.backtest.calendar import EXCHANGE_SESSIONS
from stonks.backtest.report import BacktestReport, compute_report
from stonks.backtest.trades import RoundTrip, compute_trade_stats
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import bars_to_sessions, embargo_calendar_days, scoring_window
from stonks.lab.parallel import DatasetSpec, dataset_snapshot, planned_workers, run_tasks
from stonks.lab.survival.base import TuningSetup
from stonks.lab.tuning.base import tune_and_fit
from stonks.logging import get_logger
from stonks.stats.sharpe import psr, return_moments
from stonks.strategies.base import strategy_data_tickers

_log = get_logger("stonks.lab.survival.walk_forward")

#: Fewest stitched returns the PSR moments (skew, kurtosis, rho) need.
_MIN_PSR_BARS = 3
#: Lag-1 autocorrelation clip, as in the OOS test.
_RHO_CLIP = 0.95


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
    #: Minimum walk-forward efficiency (OOS / IS annualised return);
    #: ``None`` turns the gate off. Davey's useful range is 0.3–1.0.
    min_wfe: float | None = Field(default=0.5, ge=0.0, le=1.0)
    #: Also run the train × test matrix (see the module doc).
    matrix: bool = False
    matrix_train_bars: tuple[int, ...] = Field(default=(504, 756, 1008), min_length=1)
    matrix_test_bars: tuple[int, ...] = Field(default=(126, 252), min_length=1)
    matrix_min_pass_share: float = Field(default=2 / 3, ge=0.0, le=1.0)
    #: Worker processes for the folds; ``None`` means
    #: ``lab.parallel.default_max_workers()``, 1 runs them in-process.
    max_workers: int | None = Field(default=None, ge=1)
    #: Root seed of the per-fold global RNG seeds (``lab.parallel``).
    seed: int | None = 0

    def resolved_test_days(self, val_window: tuple[date, date]) -> int:
        """``test_days``, or the dataset's validation window split evenly
        over the folds — so the OOS windows cover the same days the
        runner's own tuning never saw."""
        if self.test_days is not None:
            return self.test_days
        val_start, val_end = val_window
        return max(1, ((val_end - val_start).days + 1) // self.n_splits)

    def folds_for(self, dataset: Any, strategy: Any = None) -> list[WalkForwardFold]:
        """The folds this config lays over ``dataset``'s full window, with
        the embargo ``strategy`` needs on ``dataset``."""
        sessions = _window_sessions(dataset)
        if sessions:
            val_start, val_end = scoring_window(dataset, strategy)
            n_val = sum(1 for s in sessions if val_start <= s <= val_end)
            return session_walk_forward_folds(
                sessions,
                n_splits=self.n_splits,
                test_sessions=self.test_days or max(1, n_val // self.n_splits),
                train_sessions=self.train_days,
                anchored=self.anchored,
                embargo_sessions=_embargo_sessions(dataset, strategy),
            )
        return walk_forward_folds(
            dataset.start,
            dataset.end,
            n_splits=self.n_splits,
            test_days=self.resolved_test_days(scoring_window(dataset, strategy)),
            train_days=self.train_days,
            anchored=self.anchored,
            embargo_days=_embargo_days(dataset, strategy),
        )

    def matrix_cells(
        self, dataset: Any, strategy: Any = None
    ) -> list[tuple[int, int, list[WalkForwardFold]]]:
        """``(train_bars, test_bars, folds)`` for every matrix cell that
        fits ``dataset``; cells that do not fit are left out."""
        interval = getattr(dataset, "interval", Interval.DAY_1)
        embargo = _embargo_days(dataset, strategy)
        sessions = _window_sessions(dataset)
        cells = []
        for train_bars in self.matrix_train_bars:
            for test_bars in self.matrix_test_bars:
                try:
                    if sessions:
                        folds = session_walk_forward_folds(
                            sessions,
                            n_splits=self.n_splits,
                            test_sessions=bars_to_sessions(test_bars, interval),
                            train_sessions=bars_to_sessions(train_bars, interval),
                            anchored=self.anchored,
                            embargo_sessions=_embargo_sessions(dataset, strategy),
                        )
                        cells.append((train_bars, test_bars, folds))
                        continue
                    folds = walk_forward_folds(
                        dataset.start,
                        dataset.end,
                        n_splits=self.n_splits,
                        test_days=bars_to_calendar_days(test_bars, interval),
                        train_days=bars_to_calendar_days(train_bars, interval),
                        anchored=self.anchored,
                        embargo_days=embargo,
                    )
                except ValueError:
                    continue
                cells.append((train_bars, test_bars, folds))
        return cells


def bars_to_calendar_days(bars: int, interval: Interval) -> int:
    """Calendar days spanned by ``bars`` bars of ``interval`` on the
    exchange-session calendar (365.25 / 252 days per session)."""
    sessions = (
        bars * EXCHANGE_SESSIONS.sessions_per_year / EXCHANGE_SESSIONS.periods_per_year(interval)
    )
    return max(1, math.ceil(round(sessions * 365.25 / EXCHANGE_SESSIONS.sessions_per_year, 9)))


def _embargo_bars(dataset: Any, strategy: Any) -> int:
    effective = getattr(dataset, "effective_embargo_bars", None)
    if callable(effective):
        return int(effective(strategy))
    return int(getattr(dataset, "embargo_bars", 0) or 0)


def _embargo_days(dataset: Any, strategy: Any) -> int:
    interval = getattr(dataset, "interval", Interval.DAY_1)
    return embargo_calendar_days(_embargo_bars(dataset, strategy), interval)


def _window_sessions(dataset: Any) -> tuple[date, ...]:
    return tuple(getattr(dataset, "window_sessions", ()) or ())


def _embargo_sessions(dataset: Any, strategy: Any) -> int:
    interval = getattr(dataset, "interval", Interval.DAY_1)
    return bars_to_sessions(_embargo_bars(dataset, strategy), interval)


def session_walk_forward_folds(
    sessions: Sequence[date],
    *,
    n_splits: int,
    test_sessions: int,
    train_sessions: int | None = None,
    anchored: bool = False,
    embargo_sessions: int = 0,
) -> list[WalkForwardFold]:
    """Lay ``n_splits`` folds over ``sessions`` (sorted trading days), the
    same geometry as :func:`walk_forward_folds` counted in whole sessions:
    ``test_sessions`` per test window, the last ending on the last session,
    ``embargo_sessions`` skipped before each one.

    Raises ``ValueError`` when the sessions cannot hold them with a
    non-empty train window."""
    if n_splits < 1 or test_sessions < 1:
        raise ValueError("n_splits and test_sessions must be >= 1")
    if embargo_sessions < 0:
        raise ValueError("embargo_sessions must be >= 0")
    first_test = len(sessions) - n_splits * test_sessions
    if first_test - embargo_sessions <= 0:
        raise ValueError(
            f"{n_splits} test windows of {test_sessions} sessions after a "
            f"{embargo_sessions}-session embargo leave no train window in {len(sessions)} sessions"
        )
    if train_sessions is None:
        train_sessions = first_test - embargo_sessions
    folds: list[WalkForwardFold] = []
    for i in range(n_splits):
        test_start = first_test + i * test_sessions
        train_end = test_start - 1 - embargo_sessions
        train_start = 0 if anchored else train_end - train_sessions + 1
        if train_start < 0:
            raise ValueError(
                f"fold {i} would train on {train_sessions} sessions before the first one; "
                "shorten train_days/test_days or reduce n_splits"
            )
        folds.append(
            WalkForwardFold(
                index=i,
                train_start=sessions[train_start],
                train_end=sessions[train_end],
                test_start=sessions[test_start],
                test_end=sessions[test_start + test_sessions - 1],
            )
        )
    return folds


def walk_forward_folds(
    start: date,
    end: date,
    *,
    n_splits: int,
    test_days: int,
    train_days: int | None = None,
    anchored: bool = False,
    embargo_days: int = 0,
) -> list[WalkForwardFold]:
    """Lay ``n_splits`` folds over ``[start, end]``; see the module doc.
    ``embargo_days`` calendar days separate each train window from its
    test window.

    Raises ``ValueError`` when the span cannot hold them with a non-empty
    train window starting on or after ``start``.
    """
    if n_splits < 1 or test_days < 1:
        raise ValueError("n_splits and test_days must be >= 1")
    if embargo_days < 0:
        raise ValueError("embargo_days must be >= 0")
    first_test = end - timedelta(days=n_splits * test_days - 1)
    if first_test - timedelta(days=embargo_days) <= start:
        raise ValueError(
            f"{n_splits} test windows of {test_days} days after a {embargo_days}-day embargo "
            f"leave no train window in [{start}, {end}]"
        )
    if train_days is None:
        train_days = (first_test - start).days - embargo_days
    folds: list[WalkForwardFold] = []
    for i in range(n_splits):
        test_start = first_test + timedelta(days=i * test_days)
        train_end = test_start - timedelta(days=1 + embargo_days)
        train_start = start if anchored else train_end - timedelta(days=train_days - 1)
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


# ---- per-fold work (runs in pool workers) ------------------------------------


@dataclass(frozen=True)
class FoldResult:
    """One fold's outcome: the tuner's best score and params, and the
    in-sample (train window) and out-of-sample (test window) backtests."""

    fold: WalkForwardFold
    best_score: float
    best_params: dict[str, Any]
    is_report: BacktestReport
    oos_report: BacktestReport


@dataclass(frozen=True)
class _FoldJob:
    """Per-worker state: what every fold task needs besides its fold."""

    dataset: Any  # the dataset, or a DatasetSpec over a snapshot of it
    strategy_cls: type
    setup: TuningSetup
    fixed: dict[str, Any]
    embargo_bars: int

    def fold_dataset(self, fold: WalkForwardFold) -> Any:
        changes: dict[str, Any] = {
            "start": fold.train_start,
            "end": fold.test_end,
            "train_end": fold.train_end,
        }
        if hasattr(self.dataset, "embargo_bars"):
            changes["embargo_bars"] = self.embargo_bars
        return dataclasses.replace(self.dataset, **changes)


def _open_job(job: _FoldJob) -> _FoldJob:
    if isinstance(job.dataset, DatasetSpec):
        return dataclasses.replace(job, dataset=job.dataset.open())
    return job


def _run_fold(job: _FoldJob, fold: WalkForwardFold) -> FoldResult:
    fold_ds = job.fold_dataset(fold)
    fitted, tuned = tune_and_fit(job.strategy_cls, fold_ds, job.setup, job.fixed)
    is_report = run_backtest(fitted, fold_ds, (fold.train_start, fold.train_end))
    oos_report = run_backtest(fitted, fold_ds, (fold.test_start, fold.test_end))
    return FoldResult(
        fold=fold,
        best_score=float(tuned.best_score),
        best_params=dict(tuned.best_params),
        is_report=is_report,
        oos_report=oos_report,
    )


def run_folds(
    strategy: Strategy,
    context: Any,
    setup: TuningSetup,
    folds: Sequence[WalkForwardFold],
    *,
    max_workers: int | None = None,
    seed: int | None = 0,
) -> list[FoldResult]:
    """Tune, fit and score every fold, in fold order, on ``max_workers``
    processes (a lake snapshot is built only when a pool is used)."""
    job = _FoldJob(
        dataset=context,
        strategy_cls=type(strategy),
        setup=setup,
        fixed=setup.retune_fixed_params(strategy),  # e.g. a wrapper's inner strategy
        embargo_bars=_embargo_bars(context, strategy),
    )
    pooled = planned_workers(len(folds), max_workers=max_workers) > 1
    extra = strategy_data_tickers(strategy)
    with dataset_snapshot(context, extra) if pooled else nullcontext(context) as shipped:
        return run_tasks(
            _run_fold,
            list(folds),
            setup=_open_job,
            payload=dataclasses.replace(job, dataset=shipped),
            max_workers=max_workers,
            root_seed=seed,
        )


# ---- stitching and efficiency --------------------------------------------------


def stitched_returns(reports: Sequence[BacktestReport]) -> np.ndarray:
    """Every report's per-bar returns, concatenated in order."""
    parts = [np.asarray(r.returns, dtype=float) for r in reports]
    return np.concatenate(parts) if parts else np.empty(0)


def stitch_reports(reports: Sequence[BacktestReport], strategy_id: str = "") -> BacktestReport:
    """One report of consecutive OOS ``reports``: their equity curves
    chained (each rescaled to start where the previous one ended, its
    first mark dropped, so the per-bar returns are exactly the
    concatenation of the folds'), their trades rescaled by the same factor
    so ``pnl / equity`` is unchanged, and every metric recomputed."""
    dates: list[date] = []
    curve: list[float] = []
    trades: list[RoundTrip] = []
    for report in reports:
        if not report.equity_curve:
            continue
        if not curve:
            scale = 1.0
            dates.extend(report.equity_dates)
            curve.extend(report.equity_curve)
        else:
            first = report.equity_curve[0]
            scale = curve[-1] / first if first else 1.0
            dates.extend(report.equity_dates[1:])
            curve.extend(v * scale for v in report.equity_curve[1:])
        trades.extend(_scaled(t, scale) for t in report.trades)
    ppy = reports[0].periods_per_year if reports else EXCHANGE_SESSIONS.sessions_per_year
    stitched = compute_report(strategy_id, dates, curve, periods_per_year=ppy)
    stats = compute_trade_stats(trades, equity_dates=dates, equity_curve=curve)
    return dataclasses.replace(stitched, trades=tuple(trades), trade_stats=stats)


def _scaled(trade: RoundTrip, scale: float) -> RoundTrip:
    if scale == 1.0:
        return trade
    return dataclasses.replace(
        trade,
        qty=trade.qty * scale,
        pnl=trade.pnl * scale,
        fees=trade.fees * scale,
        slippage_cost=trade.slippage_cost * scale,
        dividends=trade.dividends * scale,
    )


def psr0(returns: np.ndarray) -> float:
    """PSR against a zero Sharpe of per-bar ``returns`` (NaN when there
    are too few, or the Sharpe variance is degenerate)."""
    mom = return_moments(np.asarray(returns, dtype=float))
    if mom.n < _MIN_PSR_BARS:
        return float("nan")
    rho = float(np.clip(mom.rho, -_RHO_CLIP, _RHO_CLIP))
    try:
        return psr(mom.sharpe, 0.0, mom.n, mom.skew, mom.kurt, rho)
    except ValueError:
        return float("nan")


def walk_forward_efficiency(oos_annual: float, is_annual: Sequence[float]) -> float:
    """``oos_annual / mean(is_annual)`` over the finite in-sample values;
    NaN when either side is not finite or the in-sample mean is not
    positive (efficiency is meaningless without an in-sample gain)."""
    finite = [v for v in is_annual if math.isfinite(v)]
    if not finite or not math.isfinite(oos_annual):
        return float("nan")
    is_mean = sum(finite) / len(finite)
    if is_mean <= 0:
        return float("nan")
    return oos_annual / is_mean


@dataclass(frozen=True)
class _Summary:
    metrics: dict[str, float]
    failures: list[str]
    stitched: BacktestReport

    @property
    def passed(self) -> bool:
        return not self.failures


def _summarize(results: Sequence[FoldResult], cfg: WalkForwardConfig, strategy_id: str) -> _Summary:
    oos_scores = [float(getattr(r.oos_report, cfg.metric)) for r in results]
    is_scores = [r.best_score for r in results]
    finite = [s for s in oos_scores if math.isfinite(s)]
    # RS-34: no finite fold score is no evidence; NaN fails the mean gate
    mean_oos = sum(finite) / len(finite) if finite else float("nan")
    positive_share = sum(1 for s in oos_scores if s > 0) / len(oos_scores)
    finite_is = [s for s in is_scores if math.isfinite(s)]

    oos_reports = [r.oos_report for r in results]
    stitched = stitch_reports(oos_reports, strategy_id)
    returns = stitched_returns(oos_reports)
    ppy = oos_reports[0].periods_per_year
    is_cagrs = [float(r.is_report.cagr) for r in results]
    finite_is_cagr = [c for c in is_cagrs if math.isfinite(c)]
    wfe = walk_forward_efficiency(float(stitched.cagr), is_cagrs)
    metrics = {
        "oos_score_mean": mean_oos,
        "positive_share": positive_share,
        "is_score_mean": sum(finite_is) / len(finite_is) if finite_is else 0.0,
        "n_folds": float(len(results)),
        "sharpe_stitched": bt_metrics.sharpe(returns, ppy),
        "psr0_stitched": psr0(returns),
        "cagr_oos_stitched": float(stitched.cagr),
        "is_cagr_mean": (
            sum(finite_is_cagr) / len(finite_is_cagr) if finite_is_cagr else float("nan")
        ),
        "wfe": wfe,
        "n_bars_stitched": float(returns.size),
    }
    failures = []
    if positive_share < cfg.min_positive_share:
        failures.append(f"positive_share {positive_share:.2f} < {cfg.min_positive_share}")
    if not mean_oos >= cfg.min_mean_score:
        failures.append(f"oos_score_mean {mean_oos:.3f} < {cfg.min_mean_score}")
    if cfg.min_wfe is not None and not wfe >= cfg.min_wfe:
        reason = "undefined (in-sample return <= 0)" if math.isnan(wfe) else f"{wfe:.2f}"
        failures.append(f"wfe {reason} < {cfg.min_wfe}")
    return _Summary(metrics=metrics, failures=failures, stitched=stitched)


# ---- the survival test ----------------------------------------------------------


class WalkForwardTest:
    id = "walk_forward"
    #: Leaves ``stitched_oos_report`` on the dataset (``SurvivalSuite`` runs
    #: it before the tests that read it).
    publishes_to_dataset = True

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
        try:
            folds = cfg.folds_for(context, strategy)
        except ValueError as exc:
            # RS-26: a dataset too short for the folds is a failing report,
            # not a crashed lab run
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"n_folds": 0.0},
                notes=f"insufficient data: {exc}",
            )
        cells = cfg.matrix_cells(context, strategy) if cfg.matrix else []
        tasks = list(folds) + [f for _, _, cell_folds in cells for f in cell_folds]
        results = run_folds(
            strategy, context, setup, tasks, max_workers=cfg.max_workers, seed=cfg.seed
        )
        base = results[: len(folds)]
        strategy_id = str(getattr(strategy, "id", type(strategy).__name__))

        metrics: dict[str, float] = {}
        for r in base:
            i = r.fold.index
            metrics[f"fold{i}_oos_score"] = float(getattr(r.oos_report, cfg.metric))
            metrics[f"fold{i}_is_score"] = r.best_score
            metrics[f"fold{i}_max_drawdown_oos"] = float(r.oos_report.max_drawdown)
            metrics[f"fold{i}_cagr_oos"] = float(r.oos_report.cagr)
            metrics[f"fold{i}_cagr_is"] = float(r.is_report.cagr)
            _log.info(
                "walk_forward.fold",
                fold=i,
                train=[str(r.fold.train_start), str(r.fold.train_end)],
                test=[str(r.fold.test_start), str(r.fold.test_end)],
                best_params=r.best_params,
                is_score=r.best_score,
                oos_score=metrics[f"fold{i}_oos_score"],
            )
        summary = _summarize(base, cfg, strategy_id)
        metrics.update(summary.metrics)
        failures = list(summary.failures)
        _publish_stitched(context, summary.stitched)

        if cfg.matrix:
            failures += self._matrix(cells, results[len(folds) :], strategy_id, metrics)

        embargo = _embargo_days(context, strategy)
        windows = "; ".join(
            f"{f.index}: train {f.train_start}..{f.train_end} test {f.test_start}..{f.test_end}"
            for f in folds
        )
        mode = "anchored" if cfg.anchored else "rolling"
        notes = f"metric={cfg.metric}; {mode}; embargo={embargo}d; {windows}"
        if failures:
            notes += "; failed: " + "; ".join(failures)
        return SurvivalReport(test_id=self.id, passed=not failures, metrics=metrics, notes=notes)

    def _matrix(
        self,
        cells: list[tuple[int, int, list[WalkForwardFold]]],
        results: Sequence[FoldResult],
        strategy_id: str,
        metrics: dict[str, float],
    ) -> list[str]:
        cfg = self._cfg
        passed = 0
        offset = 0
        for train_bars, test_bars, cell_folds in cells:
            cell = results[offset : offset + len(cell_folds)]
            offset += len(cell_folds)
            summary = _summarize(cell, cfg, strategy_id)
            key = f"matrix_{train_bars}x{test_bars}"
            metrics[f"{key}_passed"] = 1.0 if summary.passed else 0.0
            metrics[f"{key}_wfe"] = summary.metrics["wfe"]
            passed += summary.passed
        share = passed / len(cells) if cells else 0.0
        metrics.update(
            {
                "matrix_cells": float(len(cells)),
                "matrix_cells_passed": float(passed),
                "matrix_pass_share": share,
            }
        )
        if not cells:
            return ["matrix: no train x test cell fits the dataset"]
        if share < cfg.matrix_min_pass_share:
            return [f"matrix pass share {share:.2f} < {cfg.matrix_min_pass_share:.2f}"]
        return []


def _publish_stitched(context: Any, report: BacktestReport) -> None:
    """Leave the stitched OOS report on the dataset for later tests
    (``mc_trades``); contexts that refuse attributes are left alone."""
    with suppress(AttributeError, dataclasses.FrozenInstanceError, TypeError):
        context.stitched_oos_report = report
