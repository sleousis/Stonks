"""Feature importance under purged cross-validation (roadmap 23.10,
López de Prado *AFML* ch. 8, *MLAM* ch. 6).

Three methods, all scored out of sample on purged folds
(:class:`stonks.lab.cv.PurgedKFold`), never in sample:

- **MDA** (mean decrease accuracy): fit on the train folds, score the test
  fold, then shuffle one feature's column in the test fold and score again.
  The drop is that feature's importance. Seeded, so repeatable.
- **SFI** (single feature importance): fit and score a model on each
  feature alone. No substitution effect, but blind to interactions.
- **Clustered MDA**: like MDA, but every feature of a cluster of correlated
  features is shuffled at once. Plain MDA splits the credit between twins
  (shuffling one leaves the other), so both look useless. Clusters come
  from :func:`feature_clusters` (k-means on the correlation distance, behind
  the :class:`~stonks.features.ml.Clusterer` seam) or are given.

Scores are ``neg_log_loss`` (default) or ``accuracy``, weighted by the
sample weights (average uniqueness for overlapping labels). Each table row
holds the mean over folds and its standard error.

A model strategy opts in with ``training_set(dataset)`` (a
:class:`TrainingSet` built from the training window only) and
``new_classifier()``. Research discipline: dropping features after reading
this report is a new hypothesis, so it is a new lab run and its trials
count. ``python -m stonks.lab.importance`` (``stonks lab importance``)
writes the report.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np

from stonks.features.ml import Classifier, SilhouetteKMeans
from stonks.lab.cv import PurgedKFold

__all__ = [
    "ImportanceReport",
    "ImportanceRow",
    "ImportanceTable",
    "TrainingSet",
    "clustered_mda",
    "feature_clusters",
    "feature_importance",
    "main",
    "mda",
    "sfi",
]

Scoring = Literal["neg_log_loss", "accuracy"]
Method = Literal["mda", "sfi", "clustered_mda"]
METHODS: tuple[Method, ...] = ("mda", "sfi", "clustered_mda")
_EPS = 1e-15


@dataclass(frozen=True)
class TrainingSet:
    """A model's training rows: features, 0/1 labels, each label's span
    ``[t0, t1]`` (sorted by ``t0``) and optional sample weights."""

    x: np.ndarray
    y: np.ndarray
    t0: Any
    t1: Any
    feature_names: tuple[str, ...]
    sample_weight: np.ndarray | None = None

    def __post_init__(self) -> None:
        x = np.atleast_2d(np.asarray(self.x, dtype=float))
        n = len(x)
        if x.shape[1] != len(self.feature_names):
            raise ValueError(f"{x.shape[1]} columns for {len(self.feature_names)} feature names")
        for name, value in (("y", self.y), ("t0", self.t0), ("t1", self.t1)):
            if len(np.asarray(value)) != n:
                raise ValueError(f"{name} must have one entry per row")
        if self.sample_weight is not None and len(np.asarray(self.sample_weight)) != n:
            raise ValueError("sample_weight must have one entry per row")
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", np.asarray(self.y, dtype=int))
        object.__setattr__(self, "feature_names", tuple(self.feature_names))

    @property
    def weights(self) -> np.ndarray:
        if self.sample_weight is None:
            return np.ones(len(self.y))
        return np.asarray(self.sample_weight, dtype=float)


@dataclass(frozen=True)
class ImportanceRow:
    name: str
    mean: float
    std_err: float
    members: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mean": self.mean,
            "std_err": self.std_err,
            "members": list(self.members),
        }


@dataclass(frozen=True)
class ImportanceTable:
    """One method's rows, most important first."""

    method: Method
    scoring: Scoring
    rows: tuple[ImportanceRow, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "scoring": self.scoring,
            "rows": [r.as_dict() for r in self.rows],
        }


@dataclass(frozen=True)
class ImportanceReport:
    strategy_id: str
    n_samples: int
    folds: int
    embargo_pct: float
    scoring: Scoring
    tables: tuple[ImportanceTable, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "n_samples": self.n_samples,
            "folds": self.folds,
            "embargo_pct": self.embargo_pct,
            "scoring": self.scoring,
            "tables": [t.as_dict() for t in self.tables],
        }


# ---- scoring -------------------------------------------------------------------


def _score(p: np.ndarray, y: np.ndarray, w: np.ndarray, scoring: Scoring) -> float:
    if scoring == "accuracy":
        hits = ((p > 0.5).astype(int) == y).astype(float)
        return float(np.average(hits, weights=w))
    q = np.clip(p, _EPS, 1.0 - _EPS)
    return float(np.average(y * np.log(q) + (1 - y) * np.log(1.0 - q), weights=w))


def _check_scoring(scoring: str) -> Scoring:
    if scoring not in ("neg_log_loss", "accuracy"):
        raise ValueError(f"scoring must be neg_log_loss or accuracy, got {scoring!r}")
    return scoring  # type: ignore[return-value]


def _splits(data: TrainingSet, folds: int, embargo_pct: float):
    return [
        (train, test)
        for train, test in PurgedKFold(folds, embargo_pct).split(data.t0, data.t1)
        if len(train) and len(test)
    ]


def _row(name: str, values: Sequence[float], members: Sequence[str]) -> ImportanceRow:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return ImportanceRow(name, float("nan"), float("nan"), tuple(members))
    err = float(arr.std(ddof=1) / math.sqrt(len(arr))) if len(arr) > 1 else 0.0
    return ImportanceRow(name, float(arr.mean()), err, tuple(members))


def _sorted(rows: list[ImportanceRow]) -> tuple[ImportanceRow, ...]:
    return tuple(
        sorted(rows, key=lambda r: (-(r.mean if math.isfinite(r.mean) else -1e300), r.name))
    )


# ---- the methods ------------------------------------------------------------------


def _permutation_importance(
    make_classifier: Callable[[], Classifier],
    data: TrainingSet,
    groups: Sequence[Sequence[str]],
    *,
    method: Method,
    folds: int,
    embargo_pct: float,
    scoring: Scoring,
    seed: int,
) -> ImportanceTable:
    index = {n: j for j, n in enumerate(data.feature_names)}
    cols = [[index[n] for n in g] for g in groups]
    drops: list[list[float]] = [[] for _ in groups]
    rng = np.random.default_rng(seed)
    w = data.weights
    for train, test in _splits(data, folds, embargo_pct):
        clf = make_classifier()
        clf.fit(data.x[train], data.y[train], sample_weight=w[train])
        x_test = data.x[test]
        base = _score(clf.predict_proba(x_test), data.y[test], w[test], scoring)
        for k, group_cols in enumerate(cols):
            shuffled = x_test.copy()
            order = rng.permutation(len(test))
            shuffled[:, group_cols] = x_test[order][:, group_cols]
            score = _score(clf.predict_proba(shuffled), data.y[test], w[test], scoring)
            drops[k].append(base - score)
    rows = [_row("+".join(g), d, g) for g, d in zip(groups, drops, strict=True)]
    return ImportanceTable(method, scoring, _sorted(rows))


def mda(
    make_classifier: Callable[[], Classifier],
    data: TrainingSet,
    *,
    folds: int = 5,
    embargo_pct: float = 0.01,
    scoring: str = "neg_log_loss",
    seed: int = 0,
) -> ImportanceTable:
    """Mean decrease of the out-of-fold score when one feature is shuffled."""
    return _permutation_importance(
        make_classifier,
        data,
        [[n] for n in data.feature_names],
        method="mda",
        folds=folds,
        embargo_pct=embargo_pct,
        scoring=_check_scoring(scoring),
        seed=seed,
    )


def clustered_mda(
    make_classifier: Callable[[], Classifier],
    data: TrainingSet,
    *,
    clusters: Sequence[Sequence[str]] | None = None,
    folds: int = 5,
    embargo_pct: float = 0.01,
    scoring: str = "neg_log_loss",
    seed: int = 0,
) -> ImportanceTable:
    """MDA with each cluster of features shuffled together."""
    groups = (
        [list(c) for c in clusters]
        if clusters is not None
        else feature_clusters(data.x, data.feature_names, seed=seed)
    )
    known = set(data.feature_names)
    for g in groups:
        if not g or not set(g) <= known:
            raise ValueError(f"cluster {list(g)} names unknown features")
    return _permutation_importance(
        make_classifier,
        data,
        groups,
        method="clustered_mda",
        folds=folds,
        embargo_pct=embargo_pct,
        scoring=_check_scoring(scoring),
        seed=seed,
    )


def sfi(
    make_classifier: Callable[[], Classifier],
    data: TrainingSet,
    *,
    folds: int = 5,
    embargo_pct: float = 0.01,
    scoring: str = "neg_log_loss",
) -> ImportanceTable:
    """Out-of-fold score of a model fitted on each feature alone."""
    rule = _check_scoring(scoring)
    w = data.weights
    splits = _splits(data, folds, embargo_pct)
    rows = []
    for j, name in enumerate(data.feature_names):
        scores = []
        for train, test in splits:
            clf = make_classifier()
            clf.fit(data.x[train][:, [j]], data.y[train], sample_weight=w[train])
            p = clf.predict_proba(data.x[test][:, [j]])
            scores.append(_score(p, data.y[test], w[test], rule))
        rows.append(_row(name, scores, [name]))
    return ImportanceTable("sfi", rule, _sorted(rows))


def feature_clusters(x: np.ndarray, names: Sequence[str], seed: int = 0) -> list[list[str]]:
    """Groups of correlated features: k-means (best silhouette) on the rows
    of the correlation distance ``sqrt((1 - corr) / 2)``. With fewer than
    three features each is its own cluster."""
    names = list(names)
    if len(names) < 3:
        return [[n] for n in names]
    matrix = np.asarray(x, dtype=float)
    matrix = matrix[np.all(np.isfinite(matrix), axis=1)]
    corr = np.nan_to_num(np.corrcoef(matrix, rowvar=False), nan=0.0)
    distance = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))
    labels = SilhouetteKMeans(k_min=2, k_max=len(names) - 1, seed=seed).fit(distance).labels
    groups: dict[int, list[str]] = {}
    for name, label in zip(names, labels, strict=True):
        groups.setdefault(int(label), []).append(name)
    return list(groups.values())


# ---- the lab tool ---------------------------------------------------------------


def feature_importance(
    strategy: Any,
    dataset: Any,
    *,
    methods: Sequence[str] = METHODS,
    folds: int = 5,
    embargo_pct: float = 0.01,
    scoring: str = "neg_log_loss",
    seed: int = 0,
) -> ImportanceReport:
    """Every method in ``methods`` on ``strategy.training_set(dataset)``,
    each fold fitted with a fresh ``strategy.new_classifier()``."""
    build = getattr(strategy, "training_set", None)
    make = getattr(strategy, "new_classifier", None)
    if not callable(build) or not callable(make):
        raise TypeError(
            f"{type(strategy).__name__} has no training_set(dataset) and new_classifier(): "
            "feature importance needs a model strategy"
        )
    rule = _check_scoring(scoring)
    unknown = [m for m in methods if m not in METHODS]
    if unknown:
        raise ValueError(f"unknown methods {unknown}; choose from {list(METHODS)}")
    data: TrainingSet = build(dataset)
    tables = []
    for method in methods:
        if method == "mda":
            tables.append(
                mda(make, data, folds=folds, embargo_pct=embargo_pct, scoring=rule, seed=seed)
            )
        elif method == "sfi":
            tables.append(sfi(make, data, folds=folds, embargo_pct=embargo_pct, scoring=rule))
        else:
            tables.append(
                clustered_mda(
                    make, data, folds=folds, embargo_pct=embargo_pct, scoring=rule, seed=seed
                )
            )
    return ImportanceReport(
        strategy_id=str(getattr(strategy, "id", type(strategy).__name__)),
        n_samples=len(data.y),
        folds=folds,
        embargo_pct=embargo_pct,
        scoring=rule,
        tables=tuple(tables),
    )


def main(argv: Sequence[str] | None = None, *, prog: str | None = None) -> int:
    """``prog`` names the command in usage messages (``stonks lab importance``)."""
    parser = argparse.ArgumentParser(
        prog=prog or "python -m stonks.lab.importance",
        description="Feature importance (MDA, SFI, clustered MDA) of a model strategy "
        "under purged cross-validation, on the training window only.",
    )
    parser.add_argument("--strategy", required=True, help="catalog id, class name or module:Class")
    parser.add_argument("--params", default="{}", help="strategy params as JSON")
    parser.add_argument("--tickers", required=True, help="comma-separated universe")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--train-end", type=date.fromisoformat, default=None)
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--embargo-pct", type=float, default=0.01)
    parser.add_argument("--scoring", choices=["neg_log_loss", "accuracy"], default="neg_log_loss")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--config", type=Path, default=None, help="settings TOML file")
    parser.add_argument("--lake", type=Path, default=None, help="lake file (overrides config)")
    parser.add_argument("--json", type=Path, default=None, help="write the report as JSON")
    parser.add_argument("--html", type=Path, default=None, help="write an HTML page")
    args = parser.parse_args(argv)

    from stonks.config import load_settings
    from stonks.core.interval import Interval
    from stonks.lab.catalog import resolve_strategy
    from stonks.lab.dataset import LabDataset
    from stonks.reporting.importance import render_importance_page
    from stonks.store.lake import DuckDBLake

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    strategy = resolve_strategy(args.strategy)(json.loads(args.params))
    lake_path = (
        args.lake or (load_settings(args.config) if args.config else load_settings()).lake.path
    )
    with DuckDBLake(lake_path, read_only=True) as lake:
        dataset = LabDataset(
            lake=lake,
            universe=tickers,
            start=args.start,
            end=args.end,
            interval=Interval.parse(args.interval),
            train_end=args.train_end,
        )
        report = feature_importance(
            strategy,
            dataset,
            methods=[m.strip() for m in args.methods.split(",") if m.strip()],
            folds=args.folds,
            embargo_pct=args.embargo_pct,
            scoring=args.scoring,
            seed=args.seed,
        )
    sys.stdout.write(_summary(report) + "\n")
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    if args.html:
        args.html.write_text(render_importance_page(report), encoding="utf-8")
    return 0


def _summary(report: ImportanceReport) -> str:
    lines = [f"{report.strategy_id}: {report.n_samples} samples, {report.folds} purged folds"]
    for table in report.tables:
        lines.append(f"{table.method} ({table.scoring})")
        lines.extend(f"  {r.name:<30} {r.mean:+.4f} +/- {r.std_err:.4f}" for r in table.rows)
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
