"""Cross-sectional ranking toolkit (roadmap 23.12): the pieces a model that
ranks a universe needs, apart from the model itself.

- :func:`cross_sectional_ranks` turns raw factor values into per-date
  percentile ranks centred on zero, so a feature means the same thing on
  every date and in every market regime. Missing values stay missing.
- :func:`rank_label` turns a forward return into its per-date percentile
  rank in ``(0, 1]``: the model learns the order, not the size, of the next
  period's returns.
- :func:`daily_ic` is the Spearman rank correlation of predictions and
  labels on each date (the information coefficient), and
  :func:`ic_summary` its mean, IR and hit rate.
- :func:`purged_cv_diagnostic` scores a model on purged, embargoed k-fold
  out-of-fold predictions (:class:`~stonks.lab.cv.PurgedKFold`, P9) and
  measures each feature's importance there: the drop in mean IC when that
  feature's column is shuffled inside the test fold. Importance measured on
  folds the model never saw says what the model uses out of sample, not
  what it memorised.

The diagnostic never picks anything: no hyperparameter or feature is chosen
from it, so every choice stays a lab trial in the ledger (P5).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from stonks.features.ml import Regressor
from stonks.lab.cv import PurgedKFold

__all__ = [
    "CVDiagnostic",
    "cross_sectional_ranks",
    "daily_ic",
    "ic_summary",
    "purged_cv_diagnostic",
    "rank_label",
]

_DATE_LEVEL = "timestamp"
#: Names a date needs before its IC counts.
_MIN_NAMES = 3


def cross_sectional_ranks(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """``columns`` of ``frame`` (indexed ``timestamp, ticker``) as per-date
    percentile ranks minus one half, in ``(-0.5, 0.5]``. Ties share the
    average rank. A date with one name gives it ``0.5``."""
    cols = list(columns)
    ranks = frame[cols].groupby(level=_DATE_LEVEL).rank(pct=True, method="average")
    return pd.DataFrame(ranks - 0.5, index=frame.index, columns=cols).astype(float)


def rank_label(label: pd.Series) -> pd.Series:
    """``label`` (indexed ``timestamp, ticker``) as its per-date percentile
    rank in ``(0, 1]``. Missing labels stay missing."""
    return label.groupby(level=_DATE_LEVEL).rank(pct=True, method="average").astype(float)


def daily_ic(pred: Any, label: Any, dates: Any) -> pd.Series:
    """Spearman IC of ``pred`` and ``label`` per date, indexed by date.
    Dates with fewer than three scored names or a constant side are left
    out."""
    frame = pd.DataFrame(
        {
            "date": pd.DatetimeIndex(dates),
            "pred": np.asarray(pred, dtype=float),
            "label": np.asarray(label, dtype=float),
        }
    ).dropna()
    out: dict[pd.Timestamp, float] = {}
    for day, rows in frame.groupby("date", sort=True):
        if len(rows) < _MIN_NAMES:
            continue
        a = rows["pred"].rank().to_numpy()
        b = rows["label"].rank().to_numpy()
        if np.ptp(a) == 0 or np.ptp(b) == 0:
            continue
        out[pd.Timestamp(day)] = float(np.corrcoef(a, b)[0, 1])
    return pd.Series(out, dtype=float)


def ic_summary(ic: pd.Series) -> dict[str, float]:
    """Mean IC, IC standard deviation, IR (mean over deviation), share of
    dates with a positive IC and the number of dates."""
    values = np.asarray(ic.dropna(), dtype=float)
    n = len(values)
    if n == 0:
        return {
            "ic_mean": float("nan"),
            "ic_std": float("nan"),
            "ic_ir": float("nan"),
            "ic_hit_rate": float("nan"),
            "n_dates": 0,
        }
    std = float(np.std(values, ddof=1)) if n > 1 else float("nan")
    mean = float(values.mean())
    return {
        "ic_mean": mean,
        "ic_std": std,
        "ic_ir": mean / std if std and np.isfinite(std) and std > 0 else float("nan"),
        "ic_hit_rate": float((values > 0).mean()),
        "n_dates": n,
    }


@dataclass(frozen=True)
class CVDiagnostic:
    """Out-of-fold results of :func:`purged_cv_diagnostic`."""

    #: Out-of-fold prediction per row (NaN where no fold scored it).
    oof: np.ndarray
    #: Spearman IC of the out-of-fold predictions per date.
    ic_by_date: pd.Series
    #: :func:`ic_summary` of ``ic_by_date``.
    summary: dict[str, float]
    #: Feature name -> mean drop in fold IC when the feature is shuffled.
    importance: dict[str, float]
    #: ``(train rows, test rows)`` per fold, in the caller's row order.
    folds: list[tuple[np.ndarray, np.ndarray]] = field(default_factory=list)


def purged_cv_diagnostic(
    x: np.ndarray,
    y: np.ndarray,
    dates: Any,
    t1: Any,
    make_model: Callable[[], Regressor],
    *,
    feature_names: Sequence[str],
    folds: int = 3,
    embargo_pct: float = 0.01,
    seed: int = 0,
) -> CVDiagnostic:
    """Purged k-fold out-of-fold IC and permutation importance (module doc).

    Row ``i`` is decided on ``dates[i]`` and its label is known at
    ``t1[i]``. Rows are ordered by date for the split (stable, so rows of
    one date stay together); a training row whose ``[date, t1]`` overlaps a
    test fold's span is purged, and ``embargo_pct`` of the rows after each
    test fold are dropped too."""
    if folds < 2:
        raise ValueError(f"folds must be >= 2, got {folds}")
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    names = list(feature_names)
    if x.ndim != 2 or x.shape[1] != len(names) or len(x) != len(y):
        raise ValueError("x must have one column per feature name and one row per label")
    t0 = pd.DatetimeIndex(dates).to_numpy(dtype="datetime64[ns]")
    end = pd.DatetimeIndex(t1).to_numpy(dtype="datetime64[ns]")
    order = np.argsort(t0, kind="stable")
    splits = PurgedKFold(folds, embargo_pct=embargo_pct).split(t0[order], end[order])
    rng = np.random.default_rng(seed)
    oof = np.full(len(y), np.nan)
    drops: dict[str, list[float]] = {n: [] for n in names}
    kept: list[tuple[np.ndarray, np.ndarray]] = []
    for train_pos, test_pos in splits:
        train, test = order[train_pos], order[test_pos]
        kept.append((np.sort(train), np.sort(test)))
        if len(train) == 0 or len(test) == 0:
            continue
        model = make_model()
        model.fit(x[train], y[train])
        xt = x[test]
        pred = model.predict(xt)
        oof[test] = pred
        base = _mean_ic(pred, y[test], t0[test])
        if not np.isfinite(base):
            continue
        for j, name in enumerate(names):
            shuffled = xt.copy()
            shuffled[:, j] = rng.permutation(shuffled[:, j])
            drops[name].append(base - _mean_ic(model.predict(shuffled), y[test], t0[test]))
    ic = daily_ic(oof, y, t0)
    importance = {
        n: float(np.nanmean(v)) if v and np.isfinite(v).any() else 0.0 for n, v in drops.items()
    }
    return CVDiagnostic(
        oof=oof, ic_by_date=ic, summary=ic_summary(ic), importance=importance, folds=kept
    )


def _mean_ic(pred: np.ndarray, label: np.ndarray, dates: np.ndarray) -> float:
    ic = daily_ic(pred, label, dates)
    return float(ic.mean()) if len(ic) else float("nan")
