"""Research rows a remote lab worker gets and sends back (roadmap 14.9).

A lab worker on another machine never opens the server's state DB. It runs
each job on a scratch state DB and artifacts folder, and these rows travel
over the API instead:

- the **seed** the server hands out with a claimed job: the whole trial
  ledger (``lab_runs``, ``lab_trials``), so trial counts and the deflated
  Sharpe see every earlier trial (P2), and the active strategies with their
  artifact files, which ``pool_correlation`` compares a candidate against;
- the **delta** the worker sends back: the ledger rows and trial matrices
  its job wrote, and the strategies it registered with their survival
  reports and artifact files.

Rows are plain dicts keyed by column name. An import writes only the
columns the target table has, so the two sides need the same migrations
for every column to arrive, never for the import to work.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from stonks.app.errors import ConflictError, ValidationError
from stonks.lab.trials import TRIALS_SUBDIR
from stonks.store.state import SqliteState

__all__ = [
    "AppliedRows",
    "ArtifactFile",
    "ResearchRows",
    "apply_rows",
    "build_seed",
    "collect_delta",
]


class ArtifactFile(BaseModel):
    #: Relative to the artifacts folder, ``/``-separated.
    path: str = Field(min_length=1, max_length=1024)
    content_b64: str


class ResearchRows(BaseModel):
    """Rows of the research tables plus the artifact files that go with them."""

    lab_runs: list[dict[str, Any]] = Field(default_factory=list)
    lab_trials: list[dict[str, Any]] = Field(default_factory=list)
    strategies: list[dict[str, Any]] = Field(default_factory=list)
    survival_reports: list[dict[str, Any]] = Field(default_factory=list)
    files: list[ArtifactFile] = Field(default_factory=list)


@dataclass(frozen=True)
class AppliedRows:
    lab_runs: int
    lab_trials: int
    strategies: list[str]
    files: int


def _rows(state: SqliteState, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in state.sql(sql, list(params))]


def _bundle_dir(artifacts_dir: Path, stored: str) -> tuple[str, Path]:
    """``(name sent over the wire, folder on this machine)`` for a stored
    ``artifact_path`` (relative since TO-09, absolute on older rows)."""
    path = Path(stored)
    if not path.is_absolute():
        return path.as_posix(), artifacts_dir / path
    moved = artifacts_dir / path.name
    return path.name, moved if moved.exists() else path


def _files_under(root: Path, prefix: str) -> list[ArtifactFile]:
    if not root.is_dir():
        return []
    return [
        ArtifactFile(
            path=f"{prefix}/{p.relative_to(root).as_posix()}",
            content_b64=base64.b64encode(p.read_bytes()).decode("ascii"),
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


def _strategy_rows(
    state: SqliteState, artifacts_dir: Path, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[ArtifactFile]]:
    out: list[dict[str, Any]] = []
    files: list[ArtifactFile] = []
    for row in rows:
        row = dict(row)
        stored = row.get("artifact_path")
        if stored:
            name, folder = _bundle_dir(artifacts_dir, str(stored))
            row["artifact_path"] = name
            files.extend(_files_under(folder, name))
        out.append(row)
    return out, files


def build_seed(state: SqliteState, artifacts_dir: str | Path) -> ResearchRows:
    """What a remote worker needs before it runs a lab job (module doc)."""
    artifacts = Path(artifacts_dir)
    strategies, files = _strategy_rows(
        state, artifacts, _rows(state, "SELECT * FROM strategies WHERE status = 'active'")
    )
    return ResearchRows(
        lab_runs=_rows(state, "SELECT * FROM lab_runs ORDER BY started_at, id"),
        lab_trials=_rows(state, "SELECT * FROM lab_trials ORDER BY run_id, trial_index"),
        strategies=strategies,
        files=files,
    )


def collect_delta(
    state: SqliteState, artifacts_dir: str | Path, *, seed: ResearchRows
) -> ResearchRows:
    """Everything a job wrote to the scratch stores on top of ``seed``."""
    artifacts = Path(artifacts_dir)
    seeded_runs = {str(r["id"]) for r in seed.lab_runs}
    seeded_strategies = {str(r["id"]) for r in seed.strategies}
    runs = [r for r in _rows(state, "SELECT * FROM lab_runs") if str(r["id"]) not in seeded_runs]
    run_ids = {str(r["id"]) for r in runs}
    trials = [r for r in _rows(state, "SELECT * FROM lab_trials") if str(r["run_id"]) in run_ids]
    new = [
        r for r in _rows(state, "SELECT * FROM strategies") if str(r["id"]) not in seeded_strategies
    ]
    strategies, files = _strategy_rows(state, artifacts, new)
    new_ids = {str(r["id"]) for r in strategies}
    reports = [
        {k: v for k, v in r.items() if k != "id"}
        for r in _rows(state, "SELECT * FROM survival_reports ORDER BY id")
        if str(r["strategy_id"]) in new_ids
    ]
    for run_id in sorted(run_ids):
        matrix = artifacts / TRIALS_SUBDIR / f"{run_id}.npz"
        if matrix.is_file():
            files.append(
                ArtifactFile(
                    path=f"{TRIALS_SUBDIR}/{run_id}.npz",
                    content_b64=base64.b64encode(matrix.read_bytes()).decode("ascii"),
                )
            )
    return ResearchRows(
        lab_runs=runs,
        lab_trials=trials,
        strategies=strategies,
        survival_reports=reports,
        files=files,
    )


def _columns(state: SqliteState, table: str) -> list[str]:
    return [str(r["name"]) for r in state.sql(f"PRAGMA table_info({table})")]


def _insert(state: SqliteState, table: str, row: dict[str, Any], *, or_ignore: bool) -> bool:
    known = _columns(state, table)
    cols = [c for c in known if c in row]
    if not cols:
        raise ValidationError(f"a {table} row has none of the table's columns")
    marks = ", ".join("?" * len(cols))
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    cur = state.execute(
        f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({marks})", [row[c] for c in cols]
    )
    return cur.rowcount == 1


def _safe_relative(path: str) -> PurePosixPath:
    if "\\" in path or ":" in path:
        raise ValidationError(f"artifact path {path!r} is not a relative / path")
    rel = PurePosixPath(path)
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") for p in rel.parts):
        raise ValidationError(f"artifact path {path!r} leaves the artifacts folder")
    return rel


def apply_rows(
    state: SqliteState,
    artifacts_dir: str | Path,
    rows: ResearchRows,
    *,
    keep_status: bool = False,
) -> AppliedRows:
    """Write ``rows`` into ``state`` and their files under ``artifacts_dir``.

    Idempotent, so a retried upload changes nothing: ledger rows already
    there are skipped, and so is a strategy whose id exists with the same
    class (a different class under that id is a :class:`ConflictError`).
    Imported strategies are ``shadow`` unless ``keep_status`` (the seed a
    worker loads); promotion stays with the governance path. A file is
    written only under the bundle of a strategy imported here, or as the
    trial matrix of a run in ``rows``."""
    artifacts = Path(artifacts_dir)
    allowed: set[tuple[str, ...]] = set()
    written: list[str] = []
    runs = trials = 0
    with state.transaction():
        for run in rows.lab_runs:
            if _insert(state, "lab_runs", run, or_ignore=True):
                # only a run this upload adds may bring its trial matrix
                runs += 1
                allowed.add((TRIALS_SUBDIR, f"{run['id']}.npz"))
        for trial in rows.lab_trials:
            trials += _insert(state, "lab_trials", trial, or_ignore=True)
        for strategy in rows.strategies:
            sid = str(strategy["id"])
            existing = state.sql("SELECT class_path FROM strategies WHERE id = ?", [sid])
            if existing:
                if existing[0]["class_path"] != strategy.get("class_path"):
                    raise ConflictError(f"strategy id {sid!r} is already registered")
                continue
            row = dict(strategy)
            if not keep_status:
                row["status"] = "shadow"
            stored = row.get("artifact_path")
            if stored:
                bundle = _safe_relative(str(stored)).parts
                if bundle != (sid,):
                    # a bundle is the strategy's own folder, never another
                    # strategy's (or the trial matrices)
                    raise ValidationError(f"strategy {sid!r} has artifact path {stored!r}")
                allowed.add(bundle)
            _insert(state, "strategies", row, or_ignore=False)
            written.append(sid)
            for report in rows.survival_reports:
                if str(report.get("strategy_id")) == sid:
                    _insert(
                        state,
                        "survival_reports",
                        {k: v for k, v in report.items() if k != "id"},
                        or_ignore=False,
                    )
        files = 0
        for item in rows.files:
            rel = _safe_relative(item.path)
            if not any(rel.parts[: len(prefix)] == prefix for prefix in allowed):
                continue  # a file of a skipped strategy, or one no row claims
            target = artifacts.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(item.content_b64, validate=True))
            files += 1
    return AppliedRows(lab_runs=runs, lab_trials=trials, strategies=written, files=files)
