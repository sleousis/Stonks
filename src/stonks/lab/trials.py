"""Trial ledger and pre-registration (BL-04).

Every lab run is recorded **before** tuning starts (strategy class,
hypothesis, premortem, tuner, objective, budget, seed, dataset, manifest),
then every trial the tuner evaluated is appended, then the verdict. Repeated
lab runs of one strategy class are trials too, so :meth:`TrialLedger.n_trials`
counts across runs. Per-bar trial returns, when the tuner provides them
(``TunerResult.trials``, BL-07), are a float32 ``T x N`` matrix saved beside
the artifacts at ``<artifacts>/_trials/<run_id>.npz``; SQLite holds only the
scores.

:class:`TrialLedger` is the one writer of ``lab_runs`` / ``lab_trials``.
Survival tests read it through :class:`LabRunContext` via an optional
``bind_run(ctx)`` hook.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

from stonks.store.state import SqliteState

if TYPE_CHECKING:  # pragma: no cover
    from stonks.lab.survival.base import TuningSetup

TrialStatus = Literal["ok", "failed"]
Verdict = Literal["pass", "fail", "error"]

TRIALS_SUBDIR = "_trials"


@dataclass(frozen=True)
class TrialRecord:
    """One tuning trial. A non-finite score is a failed trial."""

    trial_index: int
    params: dict[str, Any]
    score: float
    n_bars: int | None = None
    status: TrialStatus = "ok"

    def __post_init__(self) -> None:
        if not _finite(self.score) and self.status == "ok":
            object.__setattr__(self, "status", "failed")

    def __eq__(self, other: object) -> bool:  # NaN scores compare equal
        if not isinstance(other, TrialRecord):
            return NotImplemented
        same_score = self.score == other.score or (
            not _finite(self.score) and not _finite(other.score)
        )
        return (
            self.trial_index == other.trial_index
            and self.params == other.params
            and same_score
            and self.n_bars == other.n_bars
            and self.status == other.status
        )

    __hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True)
class TrialMatrix:
    """Per-bar trial returns: ``values`` is ``T x N`` float32 (NaN for a
    failed trial or a bar a trial did not cover); ``index`` labels the rows
    (datetime64 when the returns were dated, else bar positions)."""

    index: np.ndarray
    values: np.ndarray

    def __post_init__(self) -> None:
        values = np.asarray(self.values, dtype=np.float32)
        if values.ndim != 2:
            raise ValueError(f"values must be T x N, got shape {values.shape}")
        index = np.asarray(self.index)
        if index.shape[0] != values.shape[0]:
            raise ValueError(f"index has {index.shape[0]} rows, values {values.shape[0]}")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "index", index)


@dataclass(frozen=True)
class LabRunSpec:
    """What a lab run commits to before it sees any result."""

    strategy_class: str
    hypothesis: str | None = None
    premortem: str | None = None
    tuner: str | None = None
    objective: str | None = None
    budget: int | None = None
    seed: int | None = None
    dataset: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    #: The research session the run belongs to (roadmap 22.9), else None.
    family: str | None = None


@dataclass
class LabRunContext:
    """Handed to survival tests with a ``bind_run(ctx)`` hook before the
    suite runs (deflated Sharpe, PBO). ``ledger`` is ``None`` when the run
    is not persisted; ``trial_matrix`` is ``None`` when the tuner reported
    scores only."""

    setup: TuningSetup
    run_id: str
    ledger: TrialLedger | None
    trials: list[TrialRecord]
    trial_matrix: TrialMatrix | None
    n_trials_run: int
    n_trials_class: int
    #: Trials of every run in the same research family (roadmap 22.9); 0
    #: for a run outside any family.
    n_trials_family: int = 0
    #: Trials of the class or the family, each counted once: everything
    #: searched on the way to this result (P2). 0 when not known.
    n_trials_searched: int = 0


class TrialLedger:
    def __init__(self, state: SqliteState, artifacts_dir: Path) -> None:
        self.state = state
        self.trials_dir = Path(artifacts_dir) / TRIALS_SUBDIR

    # ---- writes ------------------------------------------------------------

    def start_run(self, spec: LabRunSpec, run_id: str | None = None) -> str:
        """Pre-register a run; returns its id."""
        rid = run_id or new_run_id()
        self.state.execute(
            """
            INSERT INTO lab_runs
                (id, strategy_class, hypothesis, premortem, tuner, objective, budget, seed,
                 dataset_json, manifest_json, started_at, family)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                rid,
                spec.strategy_class,
                spec.hypothesis,
                spec.premortem,
                spec.tuner,
                spec.objective,
                spec.budget,
                spec.seed,
                _dumps(spec.dataset),
                _dumps(spec.manifest),
                _iso_now(),
                spec.family,
            ],
        )
        return rid

    def record_trials(
        self,
        run_id: str,
        trials: Sequence[TrialRecord],
        matrix: TrialMatrix | None = None,
    ) -> None:
        """Append a run's trials in one transaction, and its per-bar return
        matrix (if any) to ``<trials_dir>/<run_id>.npz``."""
        if matrix is not None:
            self.trials_dir.mkdir(parents=True, exist_ok=True)
            index = matrix.index
            if index.dtype == object:
                index = pd.to_datetime(index).values
            np.savez_compressed(
                self.trials_dir / f"{run_id}.npz", index=index, values=matrix.values
            )
        with self.state.transaction():
            for t in trials:
                self.state.execute(
                    """
                    INSERT INTO lab_trials
                        (run_id, trial_index, params_json, score, n_bars, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        run_id,
                        t.trial_index,
                        _dumps(t.params),
                        float(t.score) if _finite(t.score) else None,
                        t.n_bars,
                        t.status,
                    ],
                )

    def finish_run(self, run_id: str, verdict: Verdict) -> None:
        self.state.execute(
            "UPDATE lab_runs SET verdict = ?, finished_at = ? WHERE id = ?",
            [verdict, _iso_now(), run_id],
        )

    def record_run(
        self,
        spec: LabRunSpec,
        trials: Sequence[TrialRecord],
        matrix: TrialMatrix | None = None,
        verdict: Verdict | None = None,
    ) -> str:
        """``start_run`` + ``record_trials`` (+ ``finish_run`` with a verdict)."""
        run_id = self.start_run(spec)
        self.record_trials(run_id, trials, matrix)
        if verdict is not None:
            self.finish_run(run_id, verdict)
        return run_id

    # ---- reads -------------------------------------------------------------

    def n_trials(self, strategy_class: str) -> int:
        """Cumulative trial count across every recorded run of the class."""
        row = self.state.sql(
            """
            SELECT COUNT(*) FROM lab_trials t JOIN lab_runs r ON r.id = t.run_id
            WHERE r.strategy_class = ?
            """,
            [strategy_class],
        )[0]
        return int(row[0])

    def n_trials_family(self, family: str) -> int:
        """Trials across every run of one research family (roadmap 22.9)."""
        row = self.state.sql(
            "SELECT COUNT(*) FROM lab_trials t JOIN lab_runs r ON r.id = t.run_id"
            " WHERE r.family = ?",
            [family],
        )[0]
        return int(row[0])

    def n_trials_searched(self, strategy_class: str, family: str | None) -> int:
        """Trials of every run of the class or of the family, each once:
        the class's past runs outside the family and the family's runs of
        other classes both count (P2)."""
        if not family:
            return self.n_trials(strategy_class)
        row = self.state.sql(
            "SELECT COUNT(*) FROM lab_trials t JOIN lab_runs r ON r.id = t.run_id"
            " WHERE r.strategy_class = ? OR r.family = ?",
            [strategy_class, family],
        )[0]
        return int(row[0])

    def unscored_runs(self, family: str) -> list[tuple[str, int]]:
        """``(run_id, budget)`` of the family's runs with no recorded trial:
        runs stopped or crashed before their trials were written."""
        rows = self.state.sql(
            "SELECT id, budget FROM lab_runs r WHERE family = ?"
            " AND NOT EXISTS (SELECT 1 FROM lab_trials t WHERE t.run_id = r.id)"
            " ORDER BY started_at, id",
            [family],
        )
        return [(str(r["id"]), int(r["budget"] or 0)) for r in rows]

    def count_unscored(self, family: str) -> int:
        """Record each unscored run's whole budget as failed trials, so a
        stopped run still counts every trial it may have tried (P2). Returns
        the trials added."""
        added = 0
        for run_id, budget in self.unscored_runs(family):
            placeholders = [
                TrialRecord(i, {}, float("nan"), status="failed") for i in range(budget)
            ]
            self.record_trials(run_id, placeholders)
            added += budget
        return added

    def run(self, run_id: str) -> dict[str, Any] | None:
        rows = self.state.sql("SELECT * FROM lab_runs WHERE id = ?", [run_id])
        return _run_dict(rows[0]) if rows else None

    def runs(self, strategy_class: str | None = None) -> list[dict[str, Any]]:
        """Runs, newest first (optionally for one class)."""
        if strategy_class is None:
            rows = self.state.sql("SELECT * FROM lab_runs ORDER BY started_at DESC, id DESC")
        else:
            rows = self.state.sql(
                "SELECT * FROM lab_runs WHERE strategy_class = ? ORDER BY started_at DESC, id DESC",
                [strategy_class],
            )
        return [_run_dict(r) for r in rows]

    def trials(self, run_id: str) -> list[TrialRecord]:
        rows = self.state.sql(
            "SELECT * FROM lab_trials WHERE run_id = ? ORDER BY trial_index", [run_id]
        )
        return [
            TrialRecord(
                trial_index=int(r["trial_index"]),
                params=json.loads(r["params_json"]),
                score=float("nan") if r["score"] is None else float(r["score"]),
                n_bars=r["n_bars"],
                status=r["status"],
            )
            for r in rows
        ]

    def trial_matrix(self, run_id: str) -> TrialMatrix | None:
        path = self.trials_dir / f"{run_id}.npz"
        if not path.exists():
            return None
        with np.load(path, allow_pickle=False) as data:
            return TrialMatrix(index=data["index"], values=data["values"])


def trials_from_tuning(tuned: Any) -> tuple[list[TrialRecord], TrialMatrix | None]:
    """Trial records from a ``TunerResult``'s ``history``, plus the per-bar
    matrix when it carries BL-07's ``trials`` (objects with ``returns`` and
    ``n_bars``, aligned with ``history``; ``None`` for a failed trial)."""
    history = list(getattr(tuned, "history", None) or [])
    outcomes = getattr(tuned, "trials", None)
    if outcomes is not None and len(outcomes) != len(history):
        outcomes = None  # misaligned: keep the scores, drop the returns
    records = []
    for i, (params, score) in enumerate(history):
        outcome = outcomes[i] if outcomes is not None else None
        n_bars = getattr(outcome, "n_bars", None)
        records.append(
            TrialRecord(
                trial_index=i,
                params=dict(params),
                score=float(score),
                n_bars=int(n_bars) if n_bars is not None else None,
            )
        )
    matrix = None
    if outcomes is not None and any(getattr(o, "returns", None) is not None for o in outcomes):
        matrix = build_trial_matrix([_dated_returns(o) for o in outcomes])
    return records, matrix


def _dated_returns(outcome: Any) -> Any:
    """An outcome's returns as a dated ``pd.Series`` when it carries a
    matching ``index`` (RS-40: the matrix is indexed by date, so trials
    covering different bars line up), else the bare returns."""
    returns = getattr(outcome, "returns", None)
    index = getattr(outcome, "index", None)
    if returns is None or index is None or isinstance(returns, pd.Series):
        return returns
    values = np.asarray(returns, dtype=float).ravel()
    stamps = pd.to_datetime(np.asarray(index))
    if len(stamps) != values.size:
        return returns
    return pd.Series(values, index=stamps)


def build_trial_matrix(returns: Sequence[Any]) -> TrialMatrix:
    """Stack per-trial return series into a ``T x N`` matrix. Dated series
    (``pd.Series``) are outer-joined on their index; plain arrays are
    aligned by position and padded with NaN. ``None`` is an all-NaN column."""
    present = [r for r in returns if r is not None]
    if present and all(isinstance(r, pd.Series) for r in present):
        frame = pd.concat(
            [
                r.rename(i) if r is not None else pd.Series(dtype=float, name=i)
                for i, r in enumerate(returns)
            ],
            axis=1,
        ).sort_index()
        frame.columns = range(len(returns))
        index = frame.index
        if not isinstance(index, pd.DatetimeIndex):
            index = pd.to_datetime(index)
        return TrialMatrix(index=index.values, values=frame.to_numpy(dtype=float))
    arrays = [None if r is None else np.asarray(r, dtype=float).ravel() for r in returns]
    t = max((a.size for a in arrays if a is not None), default=0)
    values = np.full((t, len(arrays)), np.nan)
    for j, a in enumerate(arrays):
        if a is not None:
            values[: a.size, j] = a
    return TrialMatrix(index=np.arange(t), values=values)


def new_run_id() -> str:
    """Sortable, unique: ``lab_<UTC yyyymmddThhmmssffffff>_<8 hex>``."""
    return f"lab_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}_{uuid.uuid4().hex[:8]}"


def _run_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["dataset"] = json.loads(d.pop("dataset_json") or "{}")
    d["manifest"] = json.loads(d.pop("manifest_json") or "{}")
    return d


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
